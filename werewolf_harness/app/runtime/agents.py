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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...arena import ActionSpec
from ...players.budget import BudgetExhausted
from ...players.llm.belief import BeliefState
from ...players.recovery import GatewayExhausted


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
                     "fallback_used": last.fallback_used, "steps": len(last.steps)})
    memory = (player.belief.snapshot(), player.note) if llm else None
    return AgentResult(job.game_id, job.seat, job.version, action, forced, memory, meta)


class LocalExecutor:
    """Model turns on a thread pool in this process. Good for one node; the
    Redis Streams executor (llm/queue.py) moves them to separate workers."""

    def __init__(self, workers: int = 16):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent")
        self.inflight = 0

    def submit(self, job: AgentJob, deliver: Callable[[AgentResult], None]) -> None:
        loop = asyncio.get_running_loop()
        self.inflight += 1
        fut = loop.run_in_executor(self.pool, run_agent_turn, job)

        def done(f: asyncio.Future) -> None:
            self.inflight -= 1
            try:
                res = f.result()
            except Exception as exc:              # noqa: BLE001 -- a crashed turn becomes the fallback, not a dead game
                res = AgentResult(job.game_id, job.seat, job.version, job.spec.fallback, True,
                                  meta={"crash": f"{type(exc).__name__}: {exc}"})
            deliver(res)

        fut.add_done_callback(done)

    async def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
