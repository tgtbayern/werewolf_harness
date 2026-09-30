"""One game, one asyncio task, one mailbox.

Only this task changes the Table. Everything else -- an HTTP request with a
person's move, a model result coming back from a worker, a clock firing --
becomes a message in the mailbox, and the loop takes them one at a time. That
is the whole concurrency story inside a game: a single writer, so no locks.

Every step is committed journal-first:

    ① INSERT the journal row (+ the model seat's memory) with our fencing token
    ② apply it to the in-memory world, version += 1
    ③ publish the events

Crash between ① and ②: recovery replays the row, so the step happened. Crash
before ①: the step never happened and the turn is asked again. Crash after ②
but before ③: events are derived from the journal and regenerated on replay;
a reconnecting client catches up from its last event seq. See docs/adr/0002.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import DBAPIError

from ...arena import Terminal, Turn
from ...players.human import Pending
from ...server.service import _in_plain_words
from ..obs import logging as olog
from ..storage import DuplicateStep, Fenced, Store
from .agents import AgentJob, AgentResult
from .table import Table
from .timers import Timeout, TimingWheel

log = olog.get("runtime.actor")


# ---------------------------------------------------------------- messages
@dataclass
class HumanAction:
    seat: int
    seq: int
    action: Any
    request_id: str | None
    reply: asyncio.Future


@dataclass
class TimerFired:
    version: int
    kind: str                    # human | agent


@dataclass
class Start:
    reply: asyncio.Future | None = None


@dataclass
class Stop:
    reason: str


@dataclass
class ActorHooks:
    on_finish: Callable[[Table], Any] | None = None      # leaderboard, metrics
    on_commit: Callable[[Table, float], Any] | None = None
    on_stale: Callable[[Table], Any] | None = None


@dataclass
class ActorStats:
    commits: int = 0
    commit_ms_total: float = 0.0
    db_retries: int = 0
    extra: dict = field(default_factory=dict)


class GameActor:
    def __init__(self, table: Table, *, store: Store, executor, wheel: TimingWheel, token: int,
                 agent_timeout_s: float, hooks: ActorHooks | None = None):
        self.t = table
        self.store = store
        self.executor = executor
        self.wheel = wheel
        self.token = token
        self.agent_timeout_s = agent_timeout_s
        self.hooks = hooks or ActorHooks()
        self.mailbox: asyncio.Queue = asyncio.Queue()
        self.bell = asyncio.Condition()
        self.task: asyncio.Task | None = None
        self.timer: Timeout | None = None
        self.stopped: str | None = None           # why the loop ended (fenced, shutdown, db, finished)
        self.stats = ActorStats()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def id(self) -> str:
        return self.t.id

    @property
    def version(self) -> int:
        return len(self.t.journal)

    # ------------------------------------------------------------ lifecycle
    def launch(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.task = self._loop.create_task(self._run(), name=f"game-{self.id}")

    async def stop(self, reason: str = "shutdown") -> None:
        if self.task and not self.task.done():
            self.mailbox.put_nowait(Stop(reason))
            try:
                await asyncio.wait_for(self.task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()
        self._cancel_timer()

    def post(self, msg: Any) -> None:
        """Thread-safe: executors and timers call this from wherever they run."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.mailbox.put_nowait, msg)

    async def _run(self) -> None:
        tok = olog.bind(game_id=self.id)
        try:
            if self.t.state == "running":
                await self._prompt()
            while self.stopped is None:
                msg = await self.mailbox.get()
                await self._handle(msg)
        except Exception as exc:                          # noqa: BLE001 -- a crash is recorded, not swallowed
            log.exception("actor crashed")
            self.t.state, self.t.error = "crashed", f"{type(exc).__name__}: {exc}"
            self.t.acting = None
            await self._safe_set_state()
            await self._notify(self.t.emit("error", message=self.t.error))
            self.stopped = "crashed"
        finally:
            self._cancel_timer()
            olog.unbind(tok)

    async def _handle(self, msg: Any) -> None:
        if isinstance(msg, Stop):
            self.stopped = msg.reason
        elif isinstance(msg, Start):
            await self._start()
            if msg.reply and not msg.reply.done():
                msg.reply.set_result(self.t.summary())
        elif isinstance(msg, HumanAction):
            res = await self._human(msg)
            if not msg.reply.done():
                msg.reply.set_result(res)
        elif isinstance(msg, AgentResult):
            await self._agent_result(msg)
        elif isinstance(msg, TimerFired):
            await self._timer(msg)

    # ------------------------------------------------------------ public API (called from request handlers)
    async def submit(self, seat: int, seq: int, action: Any, request_id: str | None = None) -> dict:
        fut = asyncio.get_running_loop().create_future()
        self.mailbox.put_nowait(HumanAction(seat, seq, action, request_id, fut))
        return await asyncio.wait_for(fut, 30)

    async def start(self) -> dict:
        fut = asyncio.get_running_loop().create_future()
        self.mailbox.put_nowait(Start(fut))
        return await asyncio.wait_for(fut, 30)

    async def wait_events(self, after: int, timeout: float = 20.0) -> list:
        async with self.bell:
            if len(self.t.events) <= after:
                try:
                    await asyncio.wait_for(self.bell.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
        return self.t.events[after:]

    # ------------------------------------------------------------ turns
    async def _start(self) -> None:
        if self.t.state != "waiting":
            return
        self.t.state = "running"
        await self._safe_set_state()
        await self._notify(self.t.emit("phase", phase="start", seed=self.t.seed))
        await self._prompt()

    async def _prompt(self) -> None:
        """Ask whoever's turn it is. Never waits for the answer."""
        turn = self.t.turn
        if isinstance(turn, Terminal):
            await self._finish(turn)
            return
        assert isinstance(turn, Turn)
        view = self.t.env.observe(turn.actor)
        spec = self.t.env.action_space(turn.actor, turn.task)
        human = turn.actor in self.t.human_seats
        self.t.acting = {"actor": turn.actor, "task": turn.task, "since": time.time(), "human": human,
                         "model": self.t.seat_models.get(turn.actor, "真人"), "steps": []}
        ev = self.t.phase_event()
        v = self.version
        if human:
            secs = self.t.cfg.turn_seconds
            deadline = None if secs is None else time.time() + secs
            self.t.pending = Pending(v, turn.task, view, spec, deadline)
            self.t.pending_seat = turn.actor
            if secs is not None:
                self._arm(secs, TimerFired(v, "human"))
        else:
            job = AgentJob(self.id, turn.actor, v, turn.task, view, spec, self.t.memory.get(turn.actor),
                           make_player=lambda on_step, s=turn.actor, task=turn.task, v=v:
                           self.t.make_player(s, on_step=on_step, task=task, version=v),
                           on_step=self._step_from_thread,
                           lane=self.t.cfg.lane or ("human" if self.t.human_seats else "online"))
            self._arm(self.agent_timeout_s, TimerFired(v, "agent"))
            self.executor.submit(job, self.post)
        await self._notify(ev)

    async def _human(self, m: HumanAction) -> dict:
        if m.seat not in self.t.human_seats:
            return {"status": "not_a_human_seat"}
        if m.request_id and m.request_id in self.t.request_ids:
            seq = self.t.request_ids[m.request_id]
            return {"status": "duplicate", "seq": seq, "action": self.t.journal[seq].action}
        if m.seq < self.version and self.t.journal[m.seq].actor == m.seat:
            return {"status": "duplicate", "seq": m.seq, "action": self.t.journal[m.seq].action}
        p = self.t.pending
        if p is None or self.t.pending_seat != m.seat:
            return {"status": "not_your_turn"}
        if m.seq != p.seq:
            return {"status": "stale", "expected": p.seq}
        reason = p.spec.check(m.action)
        if reason:
            p.feedback = reason
            return {"status": "illegal", "reason": reason}
        ok = await self._commit(m.action, forced=False, request_id=m.request_id or f"h-{uuid.uuid4().hex[:12]}")
        return {"status": "accepted"} if ok else {"status": "unavailable", "reason": self.stopped}

    async def _agent_result(self, r: AgentResult) -> None:
        if r.version != self.version or self.t.pending_seat is not None or not isinstance(self.t.turn, Turn) \
                or self.t.turn.actor != r.seat:
            # the world moved on (deadline fired, game recovered, a duplicate delivery)
            self.t.stale_results += 1
            if self.hooks.on_stale:
                self.hooks.on_stale(self.t)
            return
        if r.error:
            self._cancel_timer()
            self.t.state, self.t.error = "aborted", _in_plain_words(r.error)
            self.t.acting = None
            await self._safe_set_state()
            await self._notify(self.t.emit("error", message=self.t.error))
            self.stopped = "aborted"
            return
        if r.meta.get("remote") and r.meta.get("trace"):
            for st in r.meta["trace"]:          # a worker in another process cannot stream steps; they come with the result
                self.t.traces.setdefault(r.seat, []).append({**st, "round": getattr(self.t.env, "_round", 0)})
            self.t.traces[r.seat] = self.t.traces[r.seat][-40:]
        agent_states = [(r.seat, r.memory[0], r.memory[1])] if r.memory else []
        await self._commit(r.action, forced=r.forced, agent_states=agent_states, memory=(r.seat, r.memory))

    async def _timer(self, m: TimerFired) -> None:
        if m.version != self.version or not isinstance(self.t.turn, Turn):
            return
        spec = self.t.env.action_space(self.t.turn.actor, self.t.turn.task)
        await self._commit(spec.fallback, forced=True)

    # ------------------------------------------------------------ commit
    async def _commit(self, action: Any, *, forced: bool, request_id: str | None = None,
                      agent_states: list | None = None, memory: tuple | None = None) -> bool:
        turn = self.t.turn
        assert isinstance(turn, Turn)
        t0 = time.monotonic()
        delay = 0.2
        for attempt in range(7):
            try:
                await self.store.append(self.id, token=self.token, seq=self.version, actor=turn.actor, task=turn.task,
                                        action=action, forced=forced, request_id=request_id,
                                        agent_states=agent_states or [])
                break
            except Fenced:
                # someone else owns this game now; our in-memory world must not advance
                log.warning("fenced: stopping")
                self.stopped = "fenced"
                return False
            except DuplicateStep:
                # our seq is already taken: another incarnation wrote it. Same answer as fenced.
                log.warning("duplicate step at %s: stopping", self.version)
                self.stopped = "fenced"
                return False
            except (DBAPIError, OSError, asyncio.TimeoutError, ConnectionError):
                self.stats.db_retries += 1
                if attempt == 6:
                    log.error("database unavailable: releasing the game for recovery")
                    self.stopped = "db"
                    return False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 3.0)
        # ② apply
        self._cancel_timer()
        if request_id:
            self.t.request_ids[request_id] = self.version
        if memory and memory[1] is not None:
            self.t.memory[memory[0]] = memory[1]
        self.t.pending, self.t.pending_seat, self.t.acting = None, None, None
        record, news = self.t.apply(action, forced)
        ms = (time.monotonic() - t0) * 1000
        self.stats.commits += 1
        self.stats.commit_ms_total += ms
        if self.hooks.on_commit:
            self.hooks.on_commit(self.t, ms)
        # ③ publish
        self.t.step_events(record, news)
        await self._notify(None)
        await self._prompt()
        return True

    async def _finish(self, term: Terminal) -> None:
        self.t.acting = None
        self.t.outcome = term.outcome
        self.t.state = "over"
        await self._safe_set_state()
        await self._notify(self.t.emit("end", outcome=term.outcome, health=self.t.health()))
        if self.hooks.on_finish:
            try:
                res = self.hooks.on_finish(self.t)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:                              # noqa: BLE001
                log.exception("on_finish hook failed")
        self.stopped = "finished"

    # ------------------------------------------------------------ helpers
    def _arm(self, delay: float, msg: TimerFired) -> None:
        self._cancel_timer()
        self.timer = self.wheel.schedule(delay, lambda: self.mailbox.put_nowait(msg))

    def _cancel_timer(self) -> None:
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def _step_from_thread(self, seat: int, step) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_step, seat, step)

    def _on_step(self, seat: int, step) -> None:
        row = {"n": step.step, "action": step.action, "args": step.args,
               "observation": (step.observation or "")[:300], "thought": (step.thought or "")[:300],
               "blocked": step.block_reason, "ms": step.latency_ms, "round": getattr(self.t.env, "_round", 0)}
        self.t.traces.setdefault(seat, []).append(row)
        self.t.traces[seat] = self.t.traces[seat][-40:]
        if self.t.acting and self.t.acting.get("actor") == seat:
            self.t.acting.setdefault("steps", []).append(step.action)

    async def _notify(self, _ev) -> None:
        async with self.bell:
            self.bell.notify_all()

    async def _safe_set_state(self) -> None:
        try:
            await self.store.set_state(self.id, self.t.state, outcome=self.t.outcome, error=self.t.error)
        except Exception:                                  # noqa: BLE001 -- state is re-derivable; never kill the loop for it
            logging.getLogger(__name__).exception("set_state failed")
