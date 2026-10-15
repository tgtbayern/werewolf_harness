"""Model turns, run outside the game loop.

The actor never waits on a model. It hands an AgentJob to an executor and goes
back to its mailbox (a person's move, a clock firing, a spectator joining); the
executor posts an AgentResult back when the model is done. The result carries
the `version` (journal length) it was computed against, so a result that arrives
after the world moved on -- its deadline fired and the fallback was applied, or
the game was recovered elsewhere -- is recognised as stale and dropped.

A model seat's memory (belief table + note to self) goes in with the job and
comes back with the result. Nothing is kept inside a player object between
turns, which is what lets (a) a recovered game continue with the AI remembering
what it thought, and (b) the same job run on any worker process (llm/queue.py).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...arena import ActionSpec
from ...players.budget import BudgetExhausted
from ...players.llm.belief import BeliefState
from ...players.providers.base import Cancelled, CancelToken
from ...players.recovery import GatewayExhausted
from .rules_npc import rule_action


@dataclass
class AgentJob:
    game_id: str
    seat: int
    version: int
    task: str
    view: dict
    spec: ActionSpec
    memory: tuple[dict, str] | None
    make_player: Callable[..., Any]          # (on_step) -> Player
    on_step: Callable[[int, Any], None] | None = None
    max_retries: int = 3
    lane: str = "online"                      # human (a person is waiting) | online | offline
    trace_ctx: Any = None                     # OpenTelemetry context of the turn span
    # Streaming (docs/adr/0009): where draft messages go (None = this turn is not streamed),
    # the token that stops the turn and its model call, and this execution's id -- a job
    # can run twice (a worker died, the job was claimed again) and each run streams anew.
    stream: bool = False                      # the executor wires `publish` for this job
    publish: Callable[[Any], None] | None = None
    cancel: CancelToken = field(default_factory=CancelToken)
    run: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    job_id: str = ""                          # stable across runs (what a cancel names); defaults to run

    def __post_init__(self):
        self.job_id = self.job_id or self.run


@dataclass
class DraftMsg:
    """A piece of a speech still being generated, or word that an attempt is void.

    kind "delta":   `text` follows the first `offset` characters of this attempt
    kind "abandon": this attempt will not be the turn's answer; `text` says why
    Keyed by (seat, version, run, attempt): the actor turns that into a draft id.
    """
    game_id: str
    seat: int
    version: int
    run: str
    attempt: int
    kind: str
    offset: int = 0
    text: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class DraftPump:
    """The thread side of streaming: number fragments by offset and coalesce them.

    A model streams a few tokens at a time; the table needs ~20 updates a second,
    not one per token. Fragments are held for at most `interval` (a timer flushes
    a held tail, so the last words of a speech never wait for the note call that
    follows). An abandon flushes first, so nothing of an attempt arrives after
    its abandon. Thread-safe: the model call and the timer both get here.
    """

    def __init__(self, job: AgentJob, send: Callable[[Any], None], interval: float = 0.05):
        self.job, self.send, self.interval = job, send, interval
        self._lock = threading.Lock()
        self._sent: dict[int, int] = {}        # attempt -> characters sent
        self._held: dict[int, list[str]] = {}
        self._last = 0.0
        self._timer: threading.Timer | None = None
        self.messages = 0

    def __call__(self, kind: str, attempt: int, text: str) -> None:
        with self._lock:
            if kind == "delta":
                self._held.setdefault(attempt, []).append(text)
                if time.monotonic() - self._last >= self.interval:
                    self._flush_locked()
                elif self._timer is None:
                    self._timer = threading.Timer(self.interval, self.flush)
                    self._timer.daemon = True
                    self._timer.start()
            else:
                self._flush_locked()
                self._emit(DraftMsg(self.job.game_id, self.job.seat, self.job.version, self.job.run, attempt,
                                    "abandon", self._sent.get(attempt, 0), text))

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        for attempt, parts in list(self._held.items()):
            text = "".join(parts)
            if text:
                off = self._sent.get(attempt, 0)
                self._sent[attempt] = off + len(text)
                self._emit(DraftMsg(self.job.game_id, self.job.seat, self.job.version, self.job.run, attempt,
                                    "delta", off, text))
        self._held.clear()
        self._last = time.monotonic()

    def _emit(self, msg: DraftMsg) -> None:
        self.messages += 1
        try:
            self.send(msg)
        except Exception:                                  # noqa: BLE001 -- a watcher must never break a turn
            pass


@dataclass
class AgentResult:
    game_id: str
    seat: int
    version: int
    action: Any
    forced: bool
    memory: tuple[dict, str] | None = None
    meta: dict = field(default_factory=dict)
    error: str | None = None                  # set when the game cannot continue (budget, wallet, key)


def run_agent_turn(job: AgentJob) -> AgentResult:
    """Blocking: one seat's whole turn, retries included. Runs on a worker thread/process."""
    from ..obs import tracing
    token = tracing.attach(job.trace_ctx)
    try:
        with tracing.get_tracer().start_as_current_span(
                "invoke_agent", attributes={"gen_ai.operation.name": "invoke_agent", "game.id": job.game_id,
                                            "game.seat": job.seat, "game.task": job.task,
                                            "game.version": job.version}) as span:
            res = _run_agent_turn(job)
            span.set_attribute("game.forced", res.forced)
            if res.meta.get("rule_npc"):
                span.set_attribute("game.rule_npc", True)
            return res
    finally:
        tracing.detach(token)


def _run_agent_turn(job: AgentJob) -> AgentResult:
    t0 = time.monotonic()
    player = job.make_player(job.on_step)
    llm = hasattr(player, "belief")
    pump = DraftPump(job, job.publish) if job.publish is not None and hasattr(player, "on_draft") else None
    if hasattr(player, "cancel"):
        player.cancel = job.cancel
    if pump is not None:
        player.on_draft = pump
    if llm:
        n = job.view.get("num_players") or 12
        player.belief = BeliefState(job.view["you"], list(range(1, n + 1)))
        if job.memory:
            belief, note = job.memory
            player.belief.restore(belief)
            player.note = note
    feedback = None
    try:
        for _ in range(job.max_retries):
            job.cancel.raise_if()
            action = player.act(job.view, job.task, job.spec, feedback)
            reason = job.spec.check(action)
            if reason is None:
                forced = bool(getattr(player, "last_forced", False))
                break
            feedback = reason
            if pump is not None:
                player.abandon_draft(getattr(player.last_turn, "draft_attempt", None), "spec")
        else:
            action, forced = job.spec.fallback, True
    except (BudgetExhausted, GatewayExhausted) as exc:
        if pump is not None:
            pump.flush()
        return AgentResult(job.game_id, job.seat, job.version, None, True, error=f"{type(exc).__name__}: {exc}")
    except Cancelled as exc:
        # Nobody wants this turn any more (its deadline fired, the game stopped, the turn
        # is gone). What it would still have cost is estimated for the ledger (adr/0009).
        if pump is not None:
            pump.flush()
        saved = getattr(exc, "saved", None) or {"cut": 0, "skipped": 0, "total": 0, "produced": exc.produced}
        return AgentResult(job.game_id, job.seat, job.version, None, True,
                           meta={"cancelled": exc.reason, "tokens_saved": saved, "run": job.run,
                                 "ms": int((time.monotonic() - t0) * 1000)})
    meta: dict = {"ms": int((time.monotonic() - t0) * 1000), "run": job.run}
    if pump is not None:
        pump.flush()
        meta["draft_attempt"] = getattr(getattr(player, "last_turn", None), "draft_attempt", None)
    last = getattr(player, "last_turn", None)
    if last is not None:
        meta.update({"prompt_tokens": last.prompt_tokens, "completion_tokens": last.completion_tokens,
                     "fallback_used": last.fallback_used, "steps": len(last.steps),
                     "trace": [s.to_dict() for s in last.steps][-8:]})
        if forced and last.fallback_used == "transport_failure":
            # no model could answer this turn: decide by rule from the seat's own beliefs
            action = rule_action(job.task, job.spec, player.belief.snapshot())
            meta["rule_npc"] = True
            meta["draft_attempt"] = None
    memory = (player.belief.snapshot(), player.note) if llm else None
    return AgentResult(job.game_id, job.seat, job.version, action, forced, memory, meta)


class LocalExecutor:
    """Model turns on a thread pool in this process. Good for one node; the
    Redis Streams executor (llm/queue.py) moves them to separate workers."""

    def __init__(self, workers: int = 16):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent")
        self.inflight = 0

    def cancel(self, job: AgentJob, reason: str) -> None:
        """Stop a turn in flight: the model call is cut where it stands (see CancelToken)."""
        job.cancel.cancel(reason)

    def submit(self, job: AgentJob, deliver: Callable[[AgentResult], None]) -> None:
        loop = asyncio.get_running_loop()
        self.inflight += 1
        if job.publish is None and getattr(job, "stream", False):
            job.publish = deliver                 # drafts ride the same mailbox, so they precede the result
        fut = loop.run_in_executor(self.pool, run_agent_turn, job)

        def done(f: asyncio.Future) -> None:
            self.inflight -= 1
            if f.cancelled():                     # executor shut down: the game is being handed over anyway
                return
            try:
                res = f.result()
            except Exception as exc:              # noqa: BLE001 -- a crashed turn becomes the fallback, not a dead game
                res = AgentResult(job.game_id, job.seat, job.version, job.spec.fallback, True,
                                  meta={"crash": f"{type(exc).__name__}: {exc}"})
            deliver(res)

        fut.add_done_callback(done)

    async def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
