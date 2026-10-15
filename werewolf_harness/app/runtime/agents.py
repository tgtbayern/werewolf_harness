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
import collections
import concurrent.futures
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...arena import ActionSpec
from ...players.budget import BudgetExhausted
from ...players.llm.belief import BeliefState
from ...players.recovery import GatewayExhausted
from .load import LatencyWindow, LoadSample
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
            action = player.act(job.view, job.task, job.spec, feedback)
            reason = job.spec.check(action)
            if reason is None:
                forced = bool(getattr(player, "last_forced", False))
                break
            feedback = reason
        else:
            action, forced = job.spec.fallback, True
    except (BudgetExhausted, GatewayExhausted) as exc:
        return AgentResult(job.game_id, job.seat, job.version, None, True, error=f"{type(exc).__name__}: {exc}")
    meta: dict = {"ms": int((time.monotonic() - t0) * 1000)}
    last = getattr(player, "last_turn", None)
    if last is not None:
        meta.update({"prompt_tokens": last.prompt_tokens, "completion_tokens": last.completion_tokens,
                     "fallback_used": last.fallback_used, "steps": len(last.steps),
                     "trace": [s.to_dict() for s in last.steps][-8:]})
        if forced and last.fallback_used == "transport_failure":
            # no model could answer this turn: decide by rule from the seat's own beliefs
            action = rule_action(job.task, job.spec, player.belief.snapshot())
            meta["rule_npc"] = True
    memory = (player.belief.snapshot(), player.note) if llm else None
    return AgentResult(job.game_id, job.seat, job.version, action, forced, memory, meta)


LANE_RANK = {"human": 0, "online": 1, "offline": 2}


class LocalExecutor:
    """Model turns on a thread pool in this process. Good for one node; the
    Redis Streams executor (llm/queue.py) moves them to separate workers.

    The pool's own FIFO queue is not used: jobs wait here, and a free thread takes
    the next one by this order (`fair=True`):

        1. lane          human > online > offline (a person waiting goes first)
        2. established   a game that has committed a step goes before a new game's
                         first turn -- capacity goes to games already in flight.
                         A first turn that has waited `new_grace_s` stops yielding
                         (aging), so a new game cannot starve behind a busy node.
        3. fewest running  the game with the fewest of its jobs on a thread now
        4. served longest ago  round-robin between games that tie on (3)
        5. arrival       oldest first

    (3)+(4) are per-game fair queuing. Since the engine asks every open turn at
    once, a vote puts 8 jobs from one game in the queue at the same instant; plain
    FIFO would hand that one game the next 8 threads while every other game's
    single speech turn waits behind it. Picking the game with the fewest jobs in
    service, then the one served longest ago, gives each contending game an equal
    share of threads (max-min fair) and is work-conserving: with no contention one
    game still gets all 8 threads at once, which a fixed per-game cap would forbid. `per_game_cap` adds such a cap anyway, for
    deployments that want a hard bound; 0 (the default) leaves it off.

    `fair=False` is the old behaviour (one FIFO over everything), kept so the
    overload benchmark can measure the difference.
    """

    def __init__(self, workers: int = 16, *, fair: bool = True, per_game_cap: int = 0, new_grace_s: float = 10.0):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent")
        self.capacity = workers
        self.fair = fair
        self.per_game_cap = per_game_cap
        self.new_grace_s = new_grace_s
        self.inflight = 0                                   # jobs on a thread now
        self.running: dict[str, int] = {}                   # game -> its jobs on a thread
        self.waiting: dict[str, collections.deque] = {}     # game -> its jobs not yet started (FIFO)
        self.latency = LatencyWindow()                      # per-turn service time, ms
        self.started_order: list[str] = []                  # game ids in dispatch order (tests read it)
        self._started_at: dict[int, float] = {}             # id(job) -> when it got a thread
        self._seq = 0
        self._tick = 0
        self.last_served: dict[str, int] = {}               # game -> tick its last job got a thread
        self._closed = False

    def submit(self, job: AgentJob, deliver: Callable[[AgentResult], None]) -> None:
        """Called on the event loop (by the actor). Never blocks."""
        self._seq += 1
        self.waiting.setdefault(job.game_id, collections.deque()).append((self._seq, time.monotonic(), job, deliver))
        self._pump()

    # ------------------------------------------------------------ scheduling
    def _key(self, gid: str, now: float):
        seq, t, job, _d = self.waiting[gid][0]
        if not self.fair:
            return (seq,)
        new = job.version == 0 and now - t < self.new_grace_s
        return (LANE_RANK.get(job.lane, 1), new, self.running.get(gid, 0), self.last_served.get(gid, 0), seq)

    def _next(self) -> str | None:
        best, best_key = None, None
        now = time.monotonic()
        for gid, q in self.waiting.items():
            if not q or (self.per_game_cap and self.running.get(gid, 0) >= self.per_game_cap):
                continue
            k = self._key(gid, now)
            if best_key is None or k < best_key:
                best, best_key = gid, k
        return best

    def _pump(self) -> None:
        while not self._closed and self.inflight < self.capacity:
            gid = self._next()
            if gid is None:
                return
            q = self.waiting[gid]
            _seq, _t, job, deliver = q.popleft()
            if not q:
                del self.waiting[gid]
            self._start(job, deliver)

    def _start(self, job: AgentJob, deliver: Callable[[AgentResult], None]) -> None:
        loop = asyncio.get_running_loop()
        self.inflight += 1
        self.running[job.game_id] = self.running.get(job.game_id, 0) + 1
        self.started_order.append(job.game_id)
        self._tick += 1
        self.last_served[job.game_id] = self._tick
        t0 = time.monotonic()
        key = id(job)
        self._started_at[key] = t0
        fut = loop.run_in_executor(self.pool, run_agent_turn, job)

        def done(f: asyncio.Future) -> None:
            self.inflight -= 1
            self._started_at.pop(key, None)
            n = self.running.get(job.game_id, 1) - 1
            if n > 0:
                self.running[job.game_id] = n
            else:
                self.running.pop(job.game_id, None)
                if job.game_id not in self.waiting:
                    self.last_served.pop(job.game_id, None)     # nothing left in the system: no debt
            if f.cancelled():                     # executor shut down: the game is being handed over anyway
                return
            self.latency.record((time.monotonic() - t0) * 1000)
            try:
                res = f.result()
            except Exception as exc:              # noqa: BLE001 -- a crashed turn becomes the fallback, not a dead game
                res = AgentResult(job.game_id, job.seat, job.version, job.spec.fallback, True,
                                  meta={"crash": f"{type(exc).__name__}: {exc}"})
            deliver(res)
            self._pump()

        fut.add_done_callback(done)

    # ------------------------------------------------------------ load
    def load_sample(self) -> LoadSample:
        """Depth counts every waiting job. The oldest wait counts only jobs of games in
        flight: a new game's first turn is *meant* to wait behind them (rule 2), and
        letting that wait hold the node in overload would refuse new games because
        new games are being made to wait -- a loop that only aging would break."""
        depth: dict[str, int] = {}
        oldest = None
        now = time.monotonic()
        for q in self.waiting.values():
            for _seq, t, job, _d in q:
                depth[job.lane] = depth.get(job.lane, 0) + 1
                if self.fair and job.version == 0 and now - t < self.new_grace_s:
                    continue
                oldest = t if oldest is None or t < oldest else oldest
        p50, _p95, _n = self.latency.quantiles()
        return LoadSample(depth=depth, oldest_wait_s=0.0 if oldest is None else now - oldest,
                          inflight=self.inflight, capacity=self.capacity, turn_p50_ms=p50,
                          inflight_age_s=(sum(now - t for t in self._started_at.values()) / len(self._started_at)
                                          if self._started_at else 0.0))

    async def close(self) -> None:
        self._closed = True
        self.waiting.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)
