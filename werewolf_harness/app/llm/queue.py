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
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from collections.abc import Callable

from redis.asyncio import Redis

from ...arena import Record, Turn
from ..config import Settings
from ..obs import logging as olog
from ..obs import metrics
from ..runtime.agents import AgentJob, AgentResult, run_agent_turn
from ..runtime.table import Table, TableConfig

LANES = ("human", "online", "offline")
GROUP = "workers"
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
                  "reply_to": self.node, "enq": time.time(), "lane": job.lane}
        asyncio.get_running_loop().create_task(
            self.r.xadd(jobs_key(job.lane), fields, maxlen=100_000, approximate=True))

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
        self.env_keys = env_keys or {}
        self.claim_idle_ms = claim_idle_ms or int(settings.agent_timeout_s * 1000)
        self.max_deliveries = max_deliveries
        self.done = 0
        self.stale = 0
        self.dead = 0
        self.order: list[str] = []          # lanes in the order jobs were taken (tests read this)
        self._tasks: set[asyncio.Task] = set()
        self._stop = False

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
        for t in list(self._tasks):
            t.cancel()

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
            metrics.QUEUE_WAIT_MS.labels(lane=lane).observe((time.time() - float(f["enq"])) * 1000)
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
        if len(rows) != version:
            self.stale += 1                               # the world moved on; the actor no longer wants this
            return None
        table = Table(gid, row["seed"], TableConfig.from_dict(row["config"]), self.env_keys, hub=self.hub)
        from ..runtime.runtime import Runtime
        Runtime.replay(table, [Record(r.actor, r.task, r.action, r.forced) for r in rows])
        t = table.turn
        if not isinstance(t, Turn) or t.actor != seat or t.task != f["task"]:
            self.stale += 1
            return None
        mem = json.loads(f["memory"]) if f.get("memory") else None
        job = AgentJob(gid, seat, version, t.task, table.env.observe(seat), table.env.action_space(seat, t.task),
                       tuple(mem) if mem else None,
                       make_player=lambda on_step: table.make_player(seat, task=t.task, version=version))
        res = await asyncio.to_thread(run_agent_turn, job)
        self.done += 1
        res.meta["remote"] = True
        res.meta["worker"] = self.consumer
        return {"game_id": gid, "seat": seat, "version": version, "action": res.action, "forced": res.forced,
                "memory": list(res.memory) if res.memory else None, "meta": res.meta, "error": res.error}

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
    store = Store.connect(s.database_url)
    r = Redis.from_url(s.redis_url)
    env = read_env()
    w = Worker(s, store, r, Hub(s.redis_url, env_keys=env), concurrency=s.agent_workers, env_keys=env)
    log.info("worker %s up", w.consumer)
    try:
        await w.run()
    finally:
        await store.close()
        await r.aclose()
