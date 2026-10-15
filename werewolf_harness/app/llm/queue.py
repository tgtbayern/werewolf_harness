"""Model turns through Redis Streams, run by separate worker processes.

    node (actor)                    Redis                                 worker process(es)
    ------------                    -----                                 ------------------
    submit(job) ── XADD ──▶ llm:jobs:human | online | offline ──XREADGROUP──▶ rebuild the world from
                                                                              Postgres, run the turn
    results loop ◀── XREAD ── llm:results:{node} ◀──────────── XADD ────────── then XACK the job

Why a queue at all: model turns are slow (seconds) and bursty; the game loop
must not care how many there are or where they run. Workers scale on their own,
a slow provider backs up the queue instead of the API, and a worker crash loses
nothing -- its unacknowledged jobs are claimed by another worker (XAUTOCLAIM).

Delivery is at-least-once. A job can run twice (a worker died after the model
answered but before XACK), so results are idempotent at the receiving end: the
actor accepts a result only for the exact `version` it is waiting on.

Lanes are strict priority: a turn with a person waiting at the table goes
before an AI-only game, which goes before offline evaluation. Fairness *within*
a lane needs no scheduler: a game has at most one outstanding turn (the game is
turn-based and the actor waits for it), so FIFO order is already round-robin
over games -- one chatty game cannot starve the others.

Jobs carry only (game, seat, version, task, memory). The worker rebuilds the
exact view and legal actions by replaying the journal (a few ms), so nothing
un-serialisable crosses the wire and a job can never act on a world that moved on.

Streaming (docs/adr/0009): a speech job's draft pieces go back on the same
results stream as its result (field "d" instead of "r"), written by one sender
per job in order, and the result only after the last piece -- so the node sees
them in the order they were generated and never a piece after its result. A
node that no longer wants a job (deadline, stopped game) PUBLISHes on
``llm:cancel``; the worker running it pulls the plug on its model call.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import socket
import time
from collections.abc import Callable

from redis.asyncio import Redis

from ...arena import Record, Turn
from ..config import Settings
from ..obs import logging as olog
from ..obs import metrics, tracing
from ..runtime.agents import AgentJob, AgentResult, DraftMsg, run_agent_turn
from ..runtime.table import Table, TableConfig

LANES = ("human", "online", "offline")
GROUP = "workers"
CANCEL_CHANNEL = "llm:cancel"
log = olog.get("llm.queue")


def jobs_key(lane: str) -> str:
    return f"llm:jobs:{lane}"


def results_key(node: str) -> str:
    return f"llm:results:{node}"


class StreamExecutor:
    """The node side: publish jobs, receive results, hand them to the actors."""

    def __init__(self, redis: Redis, settings: Settings):
        self.r = redis
        self.s = settings
        self.node = settings.node_id
        self.deliver: dict[str, Callable[[AgentResult], None]] = {}
        self.inflight = 0
        self._task: asyncio.Task | None = None
        self._last_id = "$"

    async def start(self) -> None:
        if self._task is None:
            self._last_id = "$"
            self._task = asyncio.create_task(self._results(), name="llm-results")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def submit(self, job: AgentJob, deliver: Callable[[AgentResult], None]) -> None:
        self.deliver[job.game_id] = deliver
        self.inflight += 1
        fields = {"game_id": job.game_id, "seat": job.seat, "version": job.version, "task": job.task,
                  "memory": json.dumps(job.memory, ensure_ascii=False) if job.memory else "",
                  "reply_to": self.node, "enq": time.time(), "lane": job.lane,
                  "stream": "1" if job.stream else "0", "job_id": job.job_id,
                  "tp": json.dumps(tracing.to_carrier(job.trace_ctx)) if job.trace_ctx is not None else ""}
        asyncio.get_running_loop().create_task(
            self.r.xadd(jobs_key(job.lane), fields, maxlen=100_000, approximate=True))

    def cancel(self, job: AgentJob, reason: str) -> None:
        """Tell whichever worker runs this job to stop it. Fire-and-forget: if nobody is
        running it (not started yet, or already done), the result is dropped as stale anyway."""
        msg = json.dumps({"job_id": job.job_id, "game_id": job.game_id, "seat": job.seat, "version": job.version,
                          "reason": reason})
        asyncio.get_running_loop().create_task(self._publish(msg))

    async def _publish(self, msg: str) -> None:
        try:
            await self.r.publish(CANCEL_CHANNEL, msg)
        except Exception:                                  # noqa: BLE001 -- a lost cancel costs tokens, not correctness
            log.warning("could not publish a cancel")

    async def _results(self) -> None:
        key = results_key(self.node)
        while True:
            try:
                got = await self.r.xread({key: self._last_id}, count=100, block=1000)
            except asyncio.CancelledError:
                raise
            except Exception:                              # noqa: BLE001 -- redis blip: back off and retry
                await asyncio.sleep(0.5)
                continue
            for _stream, entries in got or []:
                for eid, f in entries:
                    self._last_id = eid
                    piece = f.get(b"d", f.get("d"))
                    if piece is not None:                  # a draft piece, not a result
                        m = json.loads(_s(piece))
                        fn = self.deliver.get(m.get("game_id"))
                        if fn:
                            fn(DraftMsg(**{k: m[k] for k in DraftMsg.__dataclass_fields__ if k in m}))
                        continue
                    d = json.loads(_s(f[b"r"] if b"r" in f else f["r"]))
                    self.inflight = max(0, self.inflight - 1)
                    fn = self.deliver.get(d["game_id"])
                    if fn:
                        mem = d.get("memory")
                        fn(AgentResult(d["game_id"], d["seat"], d["version"], d.get("action"), d.get("forced", False),
                                       tuple(mem) if mem else None, d.get("meta") or {}, d.get("error")))


class Worker:
    """A worker process: pulls jobs by lane priority, runs them, replies, acks."""

    def __init__(self, settings: Settings, store, redis: Redis, hub, *, consumer: str | None = None,
                 concurrency: int = 8, env_keys: dict | None = None, claim_idle_ms: int | None = None,
                 max_deliveries: int = 3):
        self.s, self.store, self.r, self.hub = settings, store, redis, hub
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self.sem = asyncio.Semaphore(concurrency)
        # its own pool: asyncio.to_thread's default pool is min(32, cpus + 4) threads, which
        # load testing found capping a 2-core worker at ~6 turns in flight (~8 commits/s)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="turn")
        self.env_keys = env_keys or {}
        self.claim_idle_ms = claim_idle_ms or int(settings.agent_timeout_s * 1000)
        self.max_deliveries = max_deliveries
        self.done = 0
        self.stale = 0
        self.dead = 0
        self.order: list[str] = []          # lanes in the order jobs were taken (tests read this)
        self.waits: dict[str, list[float]] = {x: [] for x in LANES}   # ms each job sat in the queue
        self._tasks: set[asyncio.Task] = set()
        self._stop = False
        self.running: dict[str, AgentJob] = {}            # job_id -> the job this worker is running
        self.cancelled = 0
        self._cancel_early: dict[str, float] = {}         # cancels for jobs not started (here) yet
        self.drafts_sent = 0

    async def ensure_groups(self) -> None:
        for lane in LANES:
            try:
                await self.r.xgroup_create(jobs_key(lane), GROUP, id="0", mkstream=True)
            except Exception as e:                         # noqa: BLE001 -- BUSYGROUP: already there
                if "BUSYGROUP" not in str(e):
                    raise

    async def run(self) -> None:
        await self.ensure_groups()
        reclaim = asyncio.create_task(self._reclaim_loop())
        self._tasks.add(asyncio.create_task(self._cancel_loop()))
        try:
            while not self._stop:
                await self.sem.acquire()
                got = await self._next()
                if got is None:
                    self.sem.release()
                    continue
                lane, eid, fields = got
                t = asyncio.create_task(self._handle(lane, eid, fields))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
        finally:
            reclaim.cancel()

    async def stop(self) -> None:
        self._stop = True
        for job in list(self.running.values()):
            job.cancel.cancel("worker stopping")      # unacked: another worker claims it and runs it again
        for t in list(self._tasks):
            t.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)

    async def _next(self):
        # strict priority: look at each lane without blocking, highest first...
        for lane in LANES:
            got = await self.r.xreadgroup(GROUP, self.consumer, {jobs_key(lane): ">"}, count=1)
            if got:
                eid, f = got[0][1][0]
                return lane, eid, f
        # ...then wait on all of them for a while
        got = await self.r.xreadgroup(GROUP, self.consumer, {jobs_key(x): ">" for x in LANES}, count=1, block=500)
        if not got:
            return None
        stream, entries = got[0]
        lane = _s(stream).rsplit(":", 1)[1]
        eid, f = entries[0]
        return lane, eid, f

    async def _handle(self, lane: str, eid, raw: dict) -> None:
        try:
            f = {_s(k): _s(v) for k, v in raw.items()}
            self.order.append(lane)
            wait_ms = (time.time() - float(f["enq"])) * 1000
            self.waits.setdefault(lane, []).append(wait_ms)
            metrics.QUEUE_WAIT_MS.labels(lane=lane).observe(wait_ms)
            reply = await self._run(f)
            if reply is not None:
                await self.r.xadd(results_key(f["reply_to"]), {"r": json.dumps(reply, ensure_ascii=False, default=str)},
                                  maxlen=10_000, approximate=True)
            await self.r.xack(jobs_key(lane), GROUP, eid)
        finally:
            self.sem.release()

    async def _run(self, f: dict) -> dict | None:
        gid, seat, version = f["game_id"], int(f["seat"]), int(f["version"])
        row = await self.store.get_game(gid)
        if row is None or row["state"] not in ("running", "waiting"):
            self.stale += 1
            return None
        rows = await self.store.journal(gid)
        if len(rows) < version:
            self.stale += 1                               # written by an owner whose steps were never committed
            return None
        # A job may have been asked ahead of its turn (an open vote), so the world may have
        # moved since `version`. It is still wanted if the seat's turn is still open now; the
        # actor checks at commit time that the seat sees what it saw when it was asked.
        table = Table(gid, row["seed"], TableConfig.from_dict(row["config"]), self.env_keys, hub=self.hub)
        from ..runtime.runtime import Runtime
        Runtime.replay(table, [Record(r.actor, r.task, r.action, r.forced) for r in rows])
        t = table.turn
        opener = getattr(table.env, "open_turns", None)
        opened = ([t] if isinstance(t, Turn) else []) + (list(opener()) if callable(opener) and isinstance(t, Turn) else [])
        t = next((o for o in opened if o.actor == seat and o.task == f["task"]), None)
        if t is None:
            self.stale += 1                               # the world moved on; the actor no longer wants this
            return None
        mem = json.loads(f["memory"]) if f.get("memory") else None
        key = f.get("job_id") or f"{gid}:{seat}:{version}"
        if self._cancel_early.pop(key, None) is not None:
            self.stale += 1                               # cancelled while it sat in the queue
            return None
        job = AgentJob(gid, seat, version, t.task, table.env.observe(seat), table.env.action_space(seat, t.task),
                       tuple(mem) if mem else None,
                       make_player=lambda on_step: table.make_player(seat, task=t.task, version=version),
                       trace_ctx=tracing.from_carrier(json.loads(f["tp"])) if f.get("tp") else None,
                       stream=f.get("stream") == "1", job_id=key)
        loop = asyncio.get_running_loop()
        sender = None
        if job.stream:
            pieces: asyncio.Queue = asyncio.Queue()
            job.publish = lambda m: loop.call_soon_threadsafe(pieces.put_nowait, m)
            sender = asyncio.create_task(self._send_drafts(pieces, results_key(f["reply_to"])))
        self.running[key] = job
        try:
            res = await loop.run_in_executor(self.pool, run_agent_turn, job)
        finally:
            self.running.pop(key, None)
            if sender is not None:
                # the turn's own thread flushed its last piece before returning, and those
                # call_soon_threadsafe puts ran before this await resumed: the queue is complete
                pieces.put_nowait(None)
                await sender
        self.done += 1
        res.meta["remote"] = True
        res.meta["worker"] = self.consumer
        return {"game_id": gid, "seat": seat, "version": version, "action": res.action, "forced": res.forced,
                "memory": list(res.memory) if res.memory else None, "meta": res.meta, "error": res.error}

    async def _send_drafts(self, pieces: asyncio.Queue, key: str) -> None:
        """One job's draft pieces onto the results stream, strictly in order."""
        while True:
            m = await pieces.get()
            if m is None:
                return
            try:
                await self.r.xadd(key, {"d": json.dumps(m.to_dict(), ensure_ascii=False)}, maxlen=10_000,
                                  approximate=True)
                self.drafts_sent += 1
            except Exception:                              # noqa: BLE001 -- a lost piece leaves a gap the node refuses
                log.warning("could not send a draft piece")

    async def _cancel_loop(self) -> None:
        """Cancels from the nodes. Pub/sub, not a stream: a cancel nobody hears costs tokens, not correctness."""
        while not self._stop:
            ps = self.r.pubsub()
            try:
                await ps.subscribe(CANCEL_CHANNEL)
                while not self._stop:
                    msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get("type") == "message":
                        self._on_cancel(json.loads(_s(msg["data"])))
            except asyncio.CancelledError:
                raise
            except Exception:                              # noqa: BLE001 -- redis blip: resubscribe
                await asyncio.sleep(0.5)
            finally:
                try:
                    await ps.aclose()
                except Exception:                          # noqa: BLE001
                    pass

    def _on_cancel(self, d: dict) -> None:
        key = d.get("job_id") or f"{d['game_id']}:{d['seat']}:{d['version']}"
        job = self.running.get(key)
        if job is not None:
            self.cancelled += 1
            job.cancel.cancel(d.get("reason") or "cancelled")
            return
        now = time.monotonic()
        self._cancel_early[key] = now
        for k in [k for k, t0 in self._cancel_early.items() if now - t0 > 600]:
            del self._cancel_early[k]

    async def _reclaim_loop(self) -> None:
        """Jobs a dead worker took and never acknowledged: claim them after they sit idle."""
        while True:
            await asyncio.sleep(max(0.2, self.claim_idle_ms / 4000))
            for lane in LANES:
                try:
                    res = await self.r.xautoclaim(jobs_key(lane), GROUP, self.consumer, self.claim_idle_ms,
                                                  start_id="0-0", count=20)
                except Exception:                          # noqa: BLE001
                    continue
                for eid, fields in (res[1] if res else []):
                    if not fields:
                        continue
                    info = await self.r.xpending_range(jobs_key(lane), GROUP, min=eid, max=eid, count=1)
                    tries = info[0]["times_delivered"] if info else 1
                    if tries > self.max_deliveries:
                        self.dead += 1
                        await self.r.xadd("llm:dead", {k: v for k, v in fields.items()}, maxlen=10_000)
                        await self.r.xack(jobs_key(lane), GROUP, eid)
                        continue
                    await self.sem.acquire()
                    t = asyncio.create_task(self._handle(lane, eid, fields))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


async def main() -> None:
    """python -m werewolf_harness.app.llm.worker (see worker.py)."""
    from ...server.http import read_env
    from ..config import get_settings
    from ..storage import Store
    from .gateway import Hub
    s = get_settings()
    olog.setup()
    tracing.setup("worker")
    if os.environ.get("WORKER_METRICS_PORT"):
        from prometheus_client import start_http_server
        start_http_server(int(os.environ["WORKER_METRICS_PORT"]))
    store = Store.connect(s.database_url)
    r = Redis.from_url(s.redis_url)
    env = read_env()
    hub = Hub(s.redis_url, env_keys=env, tpm=s.llm_tpm, default_tpm=s.llm_default_tpm)
    w = Worker(s, store, r, hub, concurrency=s.agent_workers, env_keys=env)
    log.info("worker %s up", w.consumer)
    try:
        await w.run()
    finally:
        await store.close()
        await r.aclose()
