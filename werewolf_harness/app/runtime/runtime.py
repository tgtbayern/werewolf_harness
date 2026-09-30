"""All games on this node: creating, adopting (recovery), renewing, draining.

A node drives a game only while it holds that game's lease. Three loops:

  renew     every lease_renew_s: extend every lease we hold; a lease we failed to
            extend is gone, so that actor is stopped before it can write again
            (and if it tries anyway, the fencing token stops it in the database)
  scan      every recovery_scan_s: any live game in Postgres that nobody holds is
            adopted -- replayed from its journal, model memory restored, and the
            current turn asked again
  (startup) is just the first scan

Replay is cheap enough to do on every adoption: a whole 12-seat game (~150 steps)
replays in about 3 ms, so there are no snapshots (docs/adr/0003).
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from typing import Any

from redis.asyncio import Redis

from ...arena import Record
from ...players.providers import retired
from ..config import Settings
from ..obs import logging as olog
from ..storage import Store
from .actor import ActorHooks, GameActor
from .agents import LocalExecutor
from .lease import Leases
from .table import Table, TableConfig
from .timers import TimingWheel

log = olog.get("runtime")


class Runtime:
    def __init__(self, settings: Settings, store: Store, redis: Redis, *, executor=None,
                 env_keys: dict | None = None, hooks: ActorHooks | None = None, hub=None):
        self.s = settings
        self.store = store
        self.redis = redis
        self.node = settings.node_id
        self.leases = Leases(redis, self.node, settings.lease_ttl_ms)
        self.executor = executor or LocalExecutor(settings.agent_workers)
        self.wheel = TimingWheel(tick=0.1)
        self.env_keys = env_keys or {}
        self.hub = hub                              # app.llm.gateway.Hub (model layer), optional
        self.hooks = hooks or ActorHooks()
        self.actors: dict[str, GameActor] = {}
        self.finished: dict[str, Table] = {}        # recently ended here, kept for readers (bounded)
        self.draining = False
        self._tasks: list[asyncio.Task] = []
        self._adopting: set[str] = set()
        self.counters = {"adopted": 0, "lost_leases": 0, "created": 0}
        self.listeners: list[Callable[[str, dict], Any]] = []

    # ------------------------------------------------------------ lifecycle
    async def start(self, scan: bool = True) -> None:
        self.wheel.start()
        self._tasks.append(asyncio.create_task(self._renew_loop(), name="lease-renew"))
        if self.hub is not None:
            self._tasks.append(asyncio.create_task(self._flush_calls(), name="llm-call-flush"))
        if scan:
            await self.scan()
            self._tasks.append(asyncio.create_task(self._scan_loop(), name="recovery-scan"))

    async def shutdown(self, drain: bool = True) -> None:
        """Stop driving games and hand them over. With drain, leases are released
        so another node adopts them at its next scan instead of waiting out the TTL."""
        self.draining = True
        for t in self._tasks:
            t.cancel()
        for a in list(self.actors.values()):
            await a.stop("shutdown")
            if drain:
                await self.leases.release(a.id)
        self.actors.clear()
        await self.wheel.stop()
        await self.flush_calls()
        close = getattr(self.executor, "close", None)
        if close:
            await close()

    # ------------------------------------------------------------ games
    async def create(self, cfg: TableConfig, *, seed: int | None = None, created_by: str | None = None,
                     start: bool | None = None) -> GameActor:
        for m in cfg.models:
            why = retired(m)
            if why:
                raise ValueError(f"{m} 已经下架（{why}）")
        if self.draining:
            raise RuntimeError("this node is draining; try again")
        gid = "g" + secrets.token_hex(4)
        seed = seed if seed is not None else int.from_bytes(secrets.token_bytes(2), "big")
        table = Table(gid, seed, cfg, self.env_keys, hub=self.hub)
        await self.store.create_game(game_id=gid, variant=cfg.variant, seed=seed, config=cfg.to_dict(),
                                     state=table.state, seats=table.seat_rows(), created_by=created_by)
        if not await self.leases.acquire(gid):
            raise RuntimeError("could not take the lease on a brand-new game")
        token = await self.store.bump_fencing(gid, self.node)
        table.reset()
        actor = self._actor(table, token)
        self.counters["created"] += 1
        if table.state == "running":
            table.emit("phase", phase="start", seed=seed)
        actor.launch()
        return actor

    async def import_log(self, log: dict, *, max_tokens: int | None = None, created_by: str | None = None) -> GameActor:
        """Continue a game from an old JSON save (the pre-database format).

        The saved journal is written into a new game's journal, then the game is
        started exactly the way a recovered one is: replay, then ask the next turn.
        """
        g = log.get("game") or {}
        records = [Record(r["actor"], r["task"], r["action"], bool(r.get("forced"))) for r in log.get("journal") or []]
        if not records:
            raise ValueError("这份日志里没有回合，直接重开就行")
        if any(r.action is None or "name" not in (r.action or {}) for r in records):
            raise ValueError("这份日志的动作不全（多半是没权限那半份），换一份完整的")
        cfg = TableConfig(variant=g.get("variant", "12p"), models=list(g.get("models") or ["mock"]),
                          guard=g.get("guard", "L1+L2+L3"), playbook=g.get("playbook", "static"),
                          human_seats=list(g.get("human_seats") or []), turn_seconds=None,
                          max_tokens=max_tokens or 6_000_000, xray=bool(g.get("xray")))
        gid = "g" + secrets.token_hex(4)
        table = Table(gid, int(g["seed"]), cfg, self.env_keys, hub=self.hub)
        self.replay(table, records)                       # validate before writing anything
        await self.store.create_game(game_id=gid, variant=cfg.variant, seed=table.seed, config=cfg.to_dict(),
                                     state="running", seats=table.seat_rows(), created_by=created_by,
                                     resumed_from=g.get("id", "存档"))
        if not await self.leases.acquire(gid):
            raise RuntimeError("could not take the lease on a brand-new game")
        token = await self.store.bump_fencing(gid, self.node)
        for i, r in enumerate(records):
            await self.store.append(gid, token=token, seq=i, actor=r.actor, task=r.task, action=r.action, forced=r.forced)
        table.state = "running"
        table.resumed_from = g.get("id", "存档")
        table.emit("phase", phase="resume", round=getattr(table.env, "_round", 0), turns=len(table.journal))
        actor = self._actor(table, token)
        actor.launch()
        return actor

    def get(self, gid: str) -> GameActor | None:
        return self.actors.get(gid)

    def table(self, gid: str) -> Table | None:
        a = self.actors.get(gid)
        return a.t if a else self.finished.get(gid)

    async def load_readonly(self, gid: str) -> Table | None:
        """A game this node does not drive (ended, or held elsewhere), rebuilt for reading."""
        t = self.table(gid)
        if t is not None:
            return t
        row = await self.store.get_game(gid)
        if row is None:
            return None
        table = Table(gid, row["seed"], TableConfig.from_dict(row["config"]), self.env_keys,
                      created=row["created_at"].timestamp())
        table.state = row["state"]
        rows = await self.store.journal(gid)
        self.replay(table, [Record(r.actor, r.task, r.action, r.forced) for r in rows])
        table.outcome, table.error = row["outcome"], row["error"]
        if table.state in ("over", "aborted", "crashed"):
            self._remember(table)
        return table

    def _remember(self, table: Table) -> None:
        self.finished[table.id] = table
        while len(self.finished) > 200:
            self.finished.pop(next(iter(self.finished)))

    def _actor(self, table: Table, token: int) -> GameActor:
        a = GameActor(table, store=self.store, executor=self.executor, wheel=self.wheel, token=token,
                      agent_timeout_s=self.s.agent_timeout_s, hooks=self.hooks)
        self.actors[table.id] = a
        return a

    # ------------------------------------------------------------ recovery
    async def adopt(self, gid: str) -> GameActor | None:
        """Take over a live game nobody holds: lease, fence, replay, restore memory, continue."""
        if gid in self.actors or gid in self._adopting or self.draining:
            return None
        self._adopting.add(gid)
        try:
            if not await self.leases.acquire(gid):
                return None
            t0 = time.monotonic()
            token = await self.store.bump_fencing(gid, self.node)
            row = await self.store.get_game(gid)
            if row is None or row["state"] not in ("waiting", "running"):
                await self.leases.release(gid)
                return None
            table = Table(gid, row["seed"], TableConfig.from_dict(row["config"]), self.env_keys,
                          created=row["created_at"].timestamp(), hub=self.hub)
            table.state = row["state"]
            table.resumed_from = row.get("resumed_from")
            rows = await self.store.journal(gid)
            self.replay(table, [Record(r.actor, r.task, r.action, r.forced) for r in rows],
                        request_ids={r.request_id: r.seq for r in rows if r.request_id})
            table.memory = await self.store.agent_memory(gid)
            table.recoveries += 1
            table.emit("phase", phase="resume", round=getattr(table.env, "_round", 0), turns=len(table.journal),
                       node=self.node)
            actor = self._actor(table, token)
            actor.launch()
            self.counters["adopted"] += 1
            olog.kv(log, 20, "adopted", game_id=gid, steps=len(rows), ms=round((time.monotonic() - t0) * 1000, 1),
                    token=token, memory_seats=len(table.memory))
            for fn in self.listeners:
                fn("adopted", {"game_id": gid, "ms": (time.monotonic() - t0) * 1000})
            return actor
        finally:
            self._adopting.discard(gid)

    @staticmethod
    def replay(table: Table, journal: list[Record], request_ids: dict | None = None) -> None:
        """Rebuild world and events from the journal. No model is asked anything."""
        table.reset()
        if journal or table.state == "running":
            table.emit("phase", phase="start", seed=table.seed)
        for rec in journal:
            t = table.turn
            if not hasattr(t, "actor") or (t.actor, t.task) != (rec.actor, rec.task):
                raise ValueError(f"journal diverged at step {len(table.journal)}: env expects {t}, row is "
                                 f"{rec.actor}/{rec.task}")
            table.phase_event()
            record, news = table.apply(rec.action, rec.forced)
            table.step_events(record, news)
        table.request_ids = dict(request_ids or {})

    async def scan(self) -> int:
        n = 0
        for g in await self.store.live_games():
            if g["id"] in self.actors:
                continue
            if await self.leases.holder(g["id"]) is None and await self.adopt(g["id"]):
                n += 1
        return n

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.recovery_scan_s)
            try:
                await self.scan()
            except Exception:                              # noqa: BLE001 -- a failed scan is retried next tick
                log.exception("recovery scan failed")

    async def _flush_calls(self) -> None:
        """Model-call records are written in batches, off the hot path."""
        while True:
            await asyncio.sleep(1.0)
            await self.flush_calls()

    async def flush_calls(self) -> int:
        if self.hub is None:
            return 0
        rows = [r.to_row() for r in self.hub.sink.drain(5000)]
        if rows:
            try:
                await self.store.record_calls(rows)
            except Exception:                              # noqa: BLE001 -- accounting must not stop games
                log.exception("could not write llm_calls")
        return len(rows)

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.lease_renew_s)
            for gid, a in list(self.actors.items()):
                if a.stopped is not None:
                    # finished, fenced or out of db: hand the lease back and forget it
                    self.actors.pop(gid, None)
                    if a.stopped in ("finished", "aborted", "crashed"):
                        self._remember(a.t)
                    await self.leases.release(gid)
                    continue
                try:
                    ok = await self.leases.renew(gid)
                except Exception:                          # noqa: BLE001 -- redis blip: try again next tick
                    continue
                if not ok:
                    self.counters["lost_leases"] += 1
                    olog.kv(log, 30, "lease lost", game_id=gid)
                    self.actors.pop(gid, None)
                    await a.stop("lease_lost")
