"""Postgres access. Plain SQL through SQLAlchemy Core (async, asyncpg).

The journal write is the one that matters, and it enforces two things in the
database rather than in Python:

  * apply once     -- PRIMARY KEY (game_id, seq): a second write of the same step fails.
  * one writer     -- the caller's fencing token must equal games.fencing_token, read
                      FOR SHARE in the same transaction. A node whose lease expired
                      (GC pause, network partition, SIGSTOP) and then wakes up still
                      holds its old token; its write is refused instead of forking the game.
"""
from __future__ import annotations

import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class Fenced(Exception):
    """This node no longer owns the game: its fencing token is stale."""


class DuplicateStep(Exception):
    """The step (or the client request) is already in the journal."""


def _j(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


@dataclass
class JournalRow:
    seq: int
    actor: int
    task: str
    action: Any
    forced: bool
    request_id: str | None


class Store:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    @classmethod
    def connect(cls, url: str, pool_size: int = 10) -> Store:
        return cls(create_async_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True))

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        async with self.engine.connect() as c:
            return (await c.scalar(text("SELECT 1"))) == 1

    # ------------------------------------------------------------ users
    async def create_guest(self, name: str | None = None) -> dict:
        uid = "u_" + secrets.token_hex(6)
        name = name or f"游客{uid[-4:]}"
        async with self.engine.begin() as c:
            await c.execute(text("INSERT INTO users (id, kind, name) VALUES (:id, 'guest', :n)"), {"id": uid, "n": name})
        return {"id": uid, "name": name, "kind": "guest"}

    async def get_user(self, uid: str) -> dict | None:
        async with self.engine.connect() as c:
            r = (await c.execute(text("SELECT id, kind, name FROM users WHERE id = :id"), {"id": uid})).mappings().first()
        return dict(r) if r else None

    # ------------------------------------------------------------ games
    async def create_game(self, *, game_id: str, variant: str, seed: int, config: dict, state: str,
                          seats: Iterable[tuple[int, str, str | None]], created_by: str | None = None,
                          resumed_from: str | None = None) -> None:
        async with self.engine.begin() as c:
            await c.execute(text(
                "INSERT INTO games (id, variant, seed, config, state, created_by, resumed_from) "
                "VALUES (:id, :v, :s, CAST(:cfg AS jsonb), :st, :by, :rf)"),
                {"id": game_id, "v": variant, "s": seed, "cfg": json.dumps(config, ensure_ascii=False),
                 "st": state, "by": created_by, "rf": resumed_from})
            await c.execute(text("INSERT INTO game_seats (game_id, seat, kind, model) VALUES (:g, :seat, :k, :m)"),
                            [{"g": game_id, "seat": s, "k": k, "m": m} for s, k, m in seats])

    async def get_game(self, game_id: str) -> dict | None:
        async with self.engine.connect() as c:
            r = (await c.execute(text("SELECT * FROM games WHERE id = :id"), {"id": game_id})).mappings().first()
        if not r:
            return None
        d = dict(r)
        d["config"], d["outcome"] = _j(d["config"]), _j(d["outcome"])
        return d

    async def set_state(self, game_id: str, state: str, *, outcome: dict | None = None, error: str | None = None) -> None:
        ended = state in ("over", "aborted", "crashed")
        async with self.engine.begin() as c:
            await c.execute(text(
                "UPDATE games SET state = :st, outcome = COALESCE(CAST(:o AS jsonb), outcome), "
                "error = COALESCE(:e, error), ended_at = CASE WHEN :ended THEN now() ELSE ended_at END "
                "WHERE id = :id"),
                {"id": game_id, "st": state, "o": json.dumps(outcome, ensure_ascii=False, default=str) if outcome else None,
                 "e": error, "ended": ended})

    async def live_games(self) -> list[dict]:
        """Games somebody should be driving. Served by games_live_idx."""
        async with self.engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT id, state, owner_node, fencing_token FROM games "
                "WHERE state IN ('waiting', 'running') ORDER BY created_at"))).mappings().all()
        return [dict(r) for r in rows]

    async def recent_games(self, limit: int = 50) -> list[dict]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT id, variant, seed, config, state, outcome, error, owner_node, created_at, ended_at "
                "FROM games ORDER BY created_at DESC LIMIT :n"), {"n": limit})).mappings().all()
        return [{**dict(r), "config": _j(r["config"]), "outcome": _j(r["outcome"])} for r in rows]

    async def seats(self, game_id: str) -> list[dict]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text("SELECT seat, kind, model, user_id FROM game_seats WHERE game_id = :g ORDER BY seat"),
                                    {"g": game_id})).mappings().all()
        return [dict(r) for r in rows]

    async def claim_seat(self, game_id: str, seat: int, user_id: str) -> bool:
        """First person to claim a human seat owns it; the same person may claim it again."""
        async with self.engine.begin() as c:
            r = await c.execute(text(
                "UPDATE game_seats SET user_id = :u WHERE game_id = :g AND seat = :s AND kind = 'human' "
                "AND (user_id IS NULL OR user_id = :u) RETURNING seat"), {"g": game_id, "s": seat, "u": user_id})
            return r.first() is not None

    async def user_games(self, user_id: str, limit: int = 20) -> list[dict]:
        """Served by game_seats_user_idx (user_id, game_id)."""
        async with self.engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT g.id, g.variant, g.state, g.outcome, s.seat, g.created_at FROM game_seats s "
                "JOIN games g ON g.id = s.game_id WHERE s.user_id = :u ORDER BY g.created_at DESC LIMIT :n"),
                {"u": user_id, "n": limit})).mappings().all()
        return [{**dict(r), "outcome": _j(r["outcome"])} for r in rows]

    # ------------------------------------------------------------ ownership
    async def bump_fencing(self, game_id: str, node: str) -> int:
        """Called right after winning the lease: every earlier holder's token is now stale."""
        async with self.engine.begin() as c:
            tok = await c.scalar(text(
                "UPDATE games SET fencing_token = fencing_token + 1, owner_node = :n WHERE id = :id "
                "RETURNING fencing_token"), {"id": game_id, "n": node})
        if tok is None:
            raise KeyError(game_id)
        return int(tok)

    # ------------------------------------------------------------ journal
    async def append(self, game_id: str, *, token: int, seq: int, actor: int, task: str, action: Any,
                     forced: bool, request_id: str | None = None,
                     agent_states: Iterable[tuple[int, dict, str]] = ()) -> None:
        """Write one step (and the model seat's memory it produced) atomically.

        Raises Fenced if `token` is stale, DuplicateStep if the step or the
        request id is already there. Nothing is written in either case.
        """
        try:
            async with self.engine.begin() as c:
                current = await c.scalar(text("SELECT fencing_token FROM games WHERE id = :id FOR SHARE"), {"id": game_id})
                if current != token:
                    raise Fenced(f"{game_id}: token {token} is stale (current {current})")
                await c.execute(text(
                    "INSERT INTO journal (game_id, seq, actor, task, action, forced, request_id) "
                    "VALUES (:g, :seq, :a, :t, CAST(:act AS jsonb), :f, :rid)"),
                    {"g": game_id, "seq": seq, "a": actor, "t": task,
                     "act": json.dumps(action, ensure_ascii=False, default=str), "f": forced, "rid": request_id})
                states = [{"g": game_id, "s": s, "seq": seq, "b": json.dumps(b, ensure_ascii=False), "n": n}
                          for s, b, n in agent_states]
                if states:
                    await c.execute(text(
                        "INSERT INTO agent_state (game_id, seat, seq, belief, note) "
                        "VALUES (:g, :s, :seq, CAST(:b AS jsonb), :n)"), states)
        except IntegrityError as e:
            raise DuplicateStep(f"{game_id}#{seq} (request {request_id})") from e

    async def journal(self, game_id: str) -> list[JournalRow]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT seq, actor, task, action, forced, request_id FROM journal WHERE game_id = :g ORDER BY seq"),
                {"g": game_id})).all()
        return [JournalRow(r.seq, r.actor, r.task, _j(r.action), r.forced, r.request_id) for r in rows]

    async def step_for_request(self, game_id: str, request_id: str) -> JournalRow | None:
        async with self.engine.connect() as c:
            r = (await c.execute(text(
                "SELECT seq, actor, task, action, forced, request_id FROM journal "
                "WHERE game_id = :g AND request_id = :r"), {"g": game_id, "r": request_id})).first()
        return JournalRow(r.seq, r.actor, r.task, _j(r.action), r.forced, r.request_id) if r else None

    async def agent_memory(self, game_id: str) -> dict[int, tuple[dict, str]]:
        """The latest belief table and note of every model seat."""
        async with self.engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT DISTINCT ON (seat) seat, belief, note FROM agent_state WHERE game_id = :g "
                "ORDER BY seat, seq DESC"), {"g": game_id})).all()
        return {int(r.seat): (_j(r.belief), r.note) for r in rows}

    # ------------------------------------------------------------ model calls
    async def record_calls(self, rows: list[dict]) -> None:
        if not rows:
            return
        async with self.engine.begin() as c:
            await c.execute(text(
                "INSERT INTO llm_calls (game_id, seat, seq, model, provider, status, input_tokens, cached_tokens, "
                "output_tokens, latency_ms, cost_usd, fallback_reason) VALUES (:game_id, :seat, :seq, :model, :provider, "
                ":status, :input_tokens, :cached_tokens, :output_tokens, :latency_ms, :cost_usd, :fallback_reason)"),
                [{"game_id": None, "seat": None, "seq": None, "provider": None, "cached_tokens": 0,
                  "cost_usd": 0, "fallback_reason": None, **r} for r in rows])

    async def call_stats(self, game_id: str | None = None) -> dict:
        where = "WHERE game_id = :g" if game_id else ""
        async with self.engine.connect() as c:
            r = (await c.execute(text(
                "SELECT count(*) AS calls, coalesce(sum(input_tokens),0) AS input, coalesce(sum(cached_tokens),0) AS cached, "
                "coalesce(sum(output_tokens),0) AS output, coalesce(sum(cost_usd),0) AS cost, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms, "
                f"count(*) FILTER (WHERE fallback_reason IS NOT NULL) AS fallbacks FROM llm_calls {where}"),
                {"g": game_id})).mappings().first()
        d = dict(r)
        d["cache_hit_rate"] = round(float(d["cached"]) / float(d["input"]), 4) if d["input"] else 0.0
        return d
