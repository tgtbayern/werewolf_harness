"""The loop. Environment-agnostic: grep this file for any game word and you will find none."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .protocol import ActionSpec, Environment, Illegal, Player, Terminal, Turn


@dataclass
class Record:
    actor: str
    task: str
    action: Any
    forced: bool = False       # True when the actor exhausted retries and the fallback was used


@dataclass
class Health:
    """What a batch needs before its numbers mean anything.

    A run whose turns were mostly filled in by the harness is not a record of
    what the models did. This is computed for every episode, not on request,
    because the failure mode it guards against is a summary that never looked.
    """
    turns: int = 0
    forced: int = 0
    replays: int = 0

    @property
    def forced_rate(self) -> float:
        return self.forced / self.turns if self.turns else 0.0

    @property
    def usable(self) -> bool:
        return self.forced_rate <= 0.05

    def to_dict(self) -> dict:
        return {"turns": self.turns, "forced": self.forced, "replays": self.replays,
                "forced_rate": round(self.forced_rate, 4), "usable": self.usable}


@dataclass
class Episode:
    journal: list[Record] = field(default_factory=list)
    outcome: dict[str, Any] | None = None
    health: Health = field(default_factory=Health)


class StepLedger:
    """Idempotent state advance: the same submission applied twice advances once.

    The world is a function of the journal, so a duplicate step is not a
    duplicate write -- it is a *different world*. In-process that never happens;
    over a network it happens constantly (a retried POST, a human double-click,
    a queue redelivering a message). The key is everything the framework knows
    about a turn -- game, position in the journal, actor, task -- and a repeat
    returns the first outcome instead of touching the environment.
    """

    def __init__(self, game: str = ""):
        self.game = game
        self.seq = 0
        self.replays = 0
        self._done: dict[str, tuple[Record, Turn | Terminal]] = {}

    def key(self, turn: Turn, seq: int | None = None) -> str:
        return f"{self.game}:{self.seq if seq is None else seq}:{turn.actor}:{turn.task}"

    def apply(self, env: Environment, turn: Turn, action: Any, *,
              seq: int | None = None, forced: bool = False) -> tuple[Record, Turn | Terminal]:
        k = self.key(turn, seq)
        if k in self._done:
            self.replays += 1
            return self._done[k]
        record = Record(turn.actor, turn.task, action, forced)
        nxt = env.step(turn.actor, turn.task, action)
        self._done[k] = (record, nxt)
        self.seq += 1
        return record, nxt


def run(env: Environment, players: dict[str, Player], seed: int, max_retries: int = 3,
        ledger: StepLedger | None = None) -> Episode:
    ep = Episode()
    ledger = ledger or StepLedger()
    turn = env.reset(seed)
    while isinstance(turn, Turn):
        view = env.observe(turn.actor)
        spec = env.action_space(turn.actor, turn.task)
        action, forced = _elicit(players[turn.actor], view, turn.task, spec, max_retries)
        record, turn = ledger.apply(env, turn, action, forced=forced)
        ep.journal.append(record)
    ep.outcome = turn.outcome
    ep.health = Health(turns=len(ep.journal), forced=sum(r.forced for r in ep.journal),
                       replays=ledger.replays)
    return ep


def _elicit(player: Player, view: dict, task: str, spec: ActionSpec, max_retries: int) -> tuple[Any, bool]:
    feedback = None
    for _ in range(max_retries):
        action = player.act(view, task, spec, feedback)
        reason = spec.check(action)
        if reason is None:
            return action, False
        feedback = reason                      # the reason goes back to the actor
    return spec.fallback, True


def replay(env: Environment, seed: int, journal: list[Record], upto: int | None = None) -> Turn | Terminal:
    """world == f(seed, journal). Feed the recorded actions back; end up at the same state."""
    turn = env.reset(seed)
    for rec in journal[:upto]:
        assert isinstance(turn, Turn) and (turn.actor, turn.task) == (rec.actor, rec.task), \
            f"journal diverged at {rec}: env expected {turn}"
        turn = env.step(rec.actor, rec.task, rec.action)
    return turn
