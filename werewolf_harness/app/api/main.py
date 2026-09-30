"""HTTP + WebSocket + SSE. Thin: every handler authenticates, then asks the runtime.

Run:  uvicorn werewolf_harness.app.api.main:app  (or `python -m werewolf_harness.cli serve`)
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from redis.asyncio import Redis

from ...envs import VARIANTS
from ...players.providers import PROFILES, offered
from ...server.http import read_env
from ..auth import tokens
from ..config import Settings, get_settings
from ..obs import logging as olog
from ..obs import metrics, tracing
from ..runtime.actor import ActorHooks
from ..runtime.runtime import Runtime
from ..runtime.table import Table, TableConfig
from ..storage import Store, migrate
from .leaderboard import Leaderboard
from .limits import SlidingWindow, TokenBucket

WEB = pathlib.Path(__file__).resolve().parents[2] / "web"
DEFAULT_MAX_TOKENS = 6_000_000
log = olog.get("api")


# ------------------------------------------------------------------ wiring
class Services:
    def __init__(self, settings: Settings, store: Store, redis: Redis, runtime: Runtime):
        self.s, self.store, self.redis, self.rt = settings, store, redis, runtime
        self.limits = SlidingWindow(redis)
        self.lb = Leaderboard(redis)


def build_runtime(settings: Settings, store: Store, redis: Redis, env_keys: dict, executor=None) -> Runtime:
    lb = Leaderboard(redis)

    async def on_finish(t: Table) -> None:
        metrics.GAMES_FINISHED.labels(result=t.state).inc()
        if not t.outcome:
            return
        seats = await store.seats(t.id)
        users = {r["seat"]: r["user_id"] for r in seats if r["user_id"]}
        await lb.record(winner=t.outcome.get("winner"), roles=t.outcome.get("roles") or {},
                        seat_users=users, seat_models={s: m for s, m in t.seat_models.items() if m != "script"})

    hooks = ActorHooks(on_finish=on_finish,
                       on_commit=lambda t, ms: metrics.COMMIT_MS.observe(ms),
                       on_stale=lambda t: metrics.STALE_RESULTS.inc())
    from ..llm.gateway import Hub
    hub = Hub(settings.redis_url, env_keys=env_keys)
    if executor is None and settings.agent_executor == "streams":
        from ..llm.queue import StreamExecutor
        executor = StreamExecutor(redis, settings)
    rt = Runtime(settings, store, redis, executor=executor, env_keys=env_keys, hooks=hooks, hub=hub)
    rt.listeners.append(lambda kind, d: metrics.ADOPTIONS.inc() if kind == "adopted" else None)
    return rt


def create_app(settings: Settings | None = None, *, migrate_on_start: bool = True, executor=None,
               env_keys: dict | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        olog.setup(os.environ.get("LOG_LEVEL", "INFO"))
        tracing.setup("werewolf-api")
        if migrate_on_start:
            await migrate.aupgrade(settings.database_url)
        store = Store.connect(settings.database_url)
        redis = Redis.from_url(settings.redis_url)
        keys = env_keys if env_keys is not None else read_env()
        rt = build_runtime(settings, store, redis, keys, executor)
        app.state.svc = Services(settings, store, redis, rt)
        app.state.env_keys = keys
        await rt.start(scan=True)
        metrics.bind_runtime(rt)
        olog.kv(log, 20, "started", node=settings.node_id)
        try:
            yield
        finally:
            await rt.shutdown(drain=True)
            await store.close()
            await redis.aclose()

    app = FastAPI(title="werewolf_harness", version="0.2.0", lifespan=lifespan)
    _routes(app)
    return app


def svc(request: Request) -> Services:
    return request.app.state.svc


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def current_user(request: Request, authorization: str | None = Header(None)) -> dict:
    tok = _bearer(authorization)
    if not tok:
        raise HTTPException(401, "login first: POST /api/auth/guest")
    try:
        c = tokens.verify(svc(request).s.jwt_secret, tok)
    except tokens.AuthError as e:
        raise HTTPException(401, str(e)) from e
    return {"id": c["sub"], "claims": c}


async def optional_user(request: Request, authorization: str | None = Header(None)) -> dict | None:
    try:
        return await current_user(request, authorization)
    except HTTPException:
        return None


def seat_from(request: Request, game_id: str, seat: int | None, authorization: str | None, token: str | None) -> int | None:
    """A seat is honoured only with a seat token for this game and this seat."""
    if seat is None:
        return None
    got = tokens.seat_of(svc(request).s.jwt_secret, _bearer(authorization) or token, game_id)
    return seat if got == seat else None


# ------------------------------------------------------------------ routes
def _routes(app: FastAPI) -> None:

    @app.middleware("http")
    async def limit_and_measure(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            s = request.app.state.svc
            ip = request.client.host if request.client else "?"
            ok, retry = await s.limits.hit(f"ip:{ip}", s.s.rate_limit_per_min, 60_000)
            if not ok:
                metrics.RATE_LIMITED.labels(scope="ip").inc()
                return JSONResponse({"error": "too many requests"}, 429, headers={"Retry-After": str(max(1, retry // 1000))})
        t0 = time.perf_counter()
        resp = await call_next(request)
        route = request.scope.get("route")
        metrics.HTTP_MS.labels(route=getattr(route, "path", "other"), method=request.method,
                               code=str(resp.status_code)).observe((time.perf_counter() - t0) * 1000)
        return resp

    # ---------------------------------------------------------- static + health
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/static/{name:path}", include_in_schema=False)
    async def static(name: str):
        p = (WEB / name).resolve()
        if not str(p).startswith(str(WEB.resolve())) or not p.exists():
            raise HTTPException(404)
        return FileResponse(p)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(request: Request):
        s = svc(request)
        try:
            db = await s.store.ping()
            rd = await s.redis.ping()
        except Exception as e:                              # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, 503)
        if s.rt.draining:
            return JSONResponse({"ok": False, "draining": True}, 503)
        return {"ok": db and rd, "node": s.s.node_id, "games": len(s.rt.actors)}

    @app.get("/metrics", include_in_schema=False)
    async def prom():
        body, ctype = metrics.render()
        return PlainTextResponse(body, media_type=ctype)

    # ---------------------------------------------------------- auth
    @app.post("/api/auth/guest")
    async def guest(request: Request, body: dict | None = None):
        s = svc(request)
        u = await s.store.create_guest((body or {}).get("name"))
        return {"token": tokens.issue_user(s.s.jwt_secret, u["id"], s.s.jwt_ttl_s), "user": u}

    @app.get("/api/me")
    async def me(request: Request, user: dict = Depends(current_user)):
        s = svc(request)
        return {"user": await s.store.get_user(user["id"]), "games": await s.store.user_games(user["id"]),
                "rank": await s.lb.around(user["id"], 3)}

    # ---------------------------------------------------------- lobby
    @app.get("/api/meta")
    async def meta(request: Request):
        return {"variants": {k: {"seats": v.num_players, "sheriff": v.sheriff, "roles": v.role_counts}
                             for k, v in VARIANTS.items()},
                "models": offered(request.app.state.env_keys) + ["mock", "script", "sim", "sim-strong", "sim-cheap",
                                                                  "sim-flaky"],
                "profiles": PROFILES, "guards": ["无", "L1", "L1+L2", "L1+L2+L3"]}

    @app.get("/api/games")
    async def games(request: Request):
        s = svc(request)
        live = {gid: a.t.summary() for gid, a in s.rt.actors.items()}
        out = list(live.values())
        for g in await s.store.recent_games(50):
            if g["id"] in live:
                continue
            t = s.rt.table(g["id"])
            if t is not None:
                out.append(t.summary())
                continue
            cfg = g["config"] or {}
            n = VARIANTS.get(g["variant"]).num_players if g["variant"] in VARIANTS else 0
            out.append({"id": g["id"], "variant": g["variant"], "model": (cfg.get("models") or ["?"])[0]
                        if len(cfg.get("models") or []) == 1 else "混合", "models": cfg.get("models") or [],
                        "guard": cfg.get("guard"), "playbook": cfg.get("playbook"), "state": g["state"],
                        "seed": g["seed"], "seats": n, "human_seats": cfg.get("human_seats") or [], "alive": [],
                        "round": 0, "phase": "-", "outcome": g["outcome"], "error": g["error"],
                        "xray": cfg.get("xray", False), "node": g["owner_node"],
                        "health": {"turns": 0, "forced_rate": 0, "usable": True, "forced": 0, "tokens": 0},
                        "created": g["created_at"].timestamp()})
        return {"games": sorted(out, key=lambda x: -x.get("created", 0))}

    @app.post("/api/games", status_code=201)
    async def create(request: Request, body: dict, user: dict = Depends(current_user)):
        s = svc(request)
        ok, retry = await s.limits.hit(f"create:{user['id']}", s.s.create_limit_per_hour, 3_600_000)
        if not ok:
            metrics.RATE_LIMITED.labels(scope="create").inc()
            raise HTTPException(429, f"too many games; retry in {retry // 1000}s")
        variant = body.get("variant", "12p")
        if variant not in VARIANTS:
            raise HTTPException(400, "unknown variant")
        models = body.get("models") or body.get("model", "mock")
        cfg = TableConfig(variant=variant, models=[models] if isinstance(models, str) else list(models),
                          guard=body.get("guard", "L1+L2+L3"), playbook=body.get("playbook", "static"),
                          human_seats=[int(x) for x in body.get("human_seats", [])],
                          turn_seconds=None if body.get("turn_seconds") in (None, 0, "0") else float(body["turn_seconds"]),
                          max_tokens=int(body.get("max_tokens") or DEFAULT_MAX_TOKENS), xray=bool(body.get("xray")),
                          fallbacks=list(body.get("fallbacks") or []), routing=body.get("routing") or None,
                          lane=body.get("lane"))
        try:
            a = await s.rt.create(cfg, seed=int(body.get("seed") or 0) or None, created_by=user["id"])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        metrics.GAMES_CREATED.inc()
        return a.t.summary()

    @app.post("/api/games/resume", status_code=201)
    async def resume_saved(request: Request, body: dict, user: dict = Depends(current_user)):
        try:
            a = await svc(request).rt.import_log(body.get("log") or {}, max_tokens=body.get("max_tokens") or None,
                                                 created_by=user["id"])
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e
        return a.t.summary()

    # ---------------------------------------------------------- one game
    async def _table(request: Request, gid: str) -> Table:
        t = await svc(request).rt.load_readonly(gid)
        if t is None:
            raise HTTPException(404, "no such game")
        return t

    @app.post("/api/games/{gid}/join")
    async def join(request: Request, gid: str, body: dict, user: dict = Depends(current_user)):
        s = svc(request)
        seat = int(body.get("seat", 0))
        if not await s.store.claim_seat(gid, seat, user["id"]):
            raise HTTPException(409, "这个座位不是留给真人的，或者已经有人坐了")
        return {"ok": True, "game": gid, "seat": seat,
                "seat_token": tokens.issue_seat(s.s.jwt_secret, user["id"], gid, seat, s.s.seat_token_ttl_s)}

    @app.get("/api/games/{gid}")
    async def seat_state(request: Request, gid: str, seat: int | None = None, token: str | None = None,
                         authorization: str | None = Header(None)):
        t = await _table(request, gid)
        me_seat = seat_from(request, gid, seat, authorization, token)
        out = t.seat_state(me_seat)
        if seat is not None and me_seat is None:
            out["need_join"] = True                  # asked for a seat without that seat's token: spectator view
        return out

    @app.get("/api/games/{gid}/log")
    async def full_log(request: Request, gid: str, seat: int | None = None, token: str | None = None,
                       authorization: str | None = Header(None)):
        t = await _table(request, gid)
        return t.full_log(seat_from(request, gid, seat, authorization, token))

    def _local(request: Request, gid: str):
        a = svc(request).rt.get(gid)
        if a is None:
            raise HTTPException(409, "this game is driven by another node or has ended")
        return a

    @app.post("/api/games/{gid}/start")
    async def start(request: Request, gid: str, authorization: str | None = Header(None)):
        a = _local(request, gid)
        if tokens.seat_of(svc(request).s.jwt_secret, _bearer(authorization), gid) is None:
            raise HTTPException(403, "only a seated player can start the game")
        return await a.start()

    @app.post("/api/games/{gid}/act")
    async def act(request: Request, gid: str, body: dict, authorization: str | None = Header(None)):
        seat = int(body["seat"])
        if seat_from(request, gid, seat, authorization, None) is None:
            raise HTTPException(403, "this seat is not yours")
        a = _local(request, gid)
        return await a.submit(seat, int(body["seq"]), body["action"], body.get("request_id"))

    @app.get("/api/games/{gid}/events")
    async def sse(request: Request, gid: str, after: int = 0):
        """Spectators: the public event stream (no night actors, no thoughts)."""
        a = _local(request, gid)

        async def gen():
            cursor = after
            while True:
                if await request.is_disconnected():
                    return
                evs = await a.wait_events(cursor, timeout=15)
                for e in evs:
                    yield f"id: {e.seq}\ndata: {json.dumps(e.to_dict(), ensure_ascii=False, default=str)}\n\n"
                    cursor = e.seq + 1
                yield ": ping\n\n"
                if a.stopped and cursor >= len(a.t.events):
                    return

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get("/api/stats/llm")
    async def llm_stats(request: Request, game: str | None = None):
        s = svc(request)
        await s.rt.flush_calls()
        return await s.store.call_stats(game)

    # ---------------------------------------------------------- leaderboard
    @app.get("/api/leaderboard")
    async def leaderboard(request: Request, kind: str = "users", n: int = Query(20, le=100)):
        s = svc(request)
        return {"users": await s.lb.top_users(n)} if kind == "users" else {"models": await s.lb.models()}

    @app.get("/api/leaderboard/me")
    async def my_rank(request: Request, user: dict = Depends(current_user)):
        return await svc(request).lb.around(user["id"])

    # ---------------------------------------------------------- websocket
    @app.websocket("/ws/games/{gid}")
    async def ws(websocket: WebSocket, gid: str, token: str | None = None, last_seq: int = -1):
        await _ws_session(websocket, gid, token, last_seq)


async def _ws_session(websocket: WebSocket, gid: str, token: str | None, last_seq: int) -> None:
    """One connection: hello, then two tasks -- push events, read client messages.

        server -> hello{heartbeat_s} | event{seq,...} | pong | ack{request_id, result} | error
        client -> ping | resume{last_seq} | act{seq, action, request_id}

    The client pings every heartbeat_s (the first one after a random fraction of
    it, so a restart does not bring every client back in the same second). Two
    missed heartbeats close the connection and let the seat's presence expire.
    """
    s: Services = websocket.app.state.svc
    a = s.rt.get(gid)
    await websocket.accept()
    if a is None:
        await websocket.close(code=4404, reason="game not driven by this node")
        return
    seat = tokens.seat_of(s.s.jwt_secret, token, gid)
    metrics.WS_CONNECTIONS.inc()
    cursor = {"next": max(0, last_seq + 1)}
    last_seen = {"t": time.monotonic()}
    bucket = TokenBucket(rate=5, burst=20)
    await websocket.send_json({"op": "hello", "heartbeat_s": s.s.heartbeat_s, "node": s.s.node_id,
                               "game": gid, "seat": seat, "events": len(a.t.events)})

    async def presence() -> None:
        if seat is not None:
            await s.redis.set(f"presence:{gid}:{seat}", s.s.node_id, ex=int(s.s.heartbeat_s * 2) + 1)

    async def pusher() -> None:
        while True:
            evs = await a.wait_events(cursor["next"], timeout=s.s.heartbeat_s)
            for e in evs:
                if e.seq < cursor["next"]:
                    continue
                await websocket.send_json({"op": "event", **e.to_dict()})
                cursor["next"] = e.seq + 1
            if time.monotonic() - last_seen["t"] > s.s.heartbeat_s * 2:
                metrics.WS_HEARTBEAT_TIMEOUTS.inc()
                await websocket.close(code=4000, reason="heartbeat timeout")
                return

    async def reader() -> None:
        while True:
            msg: dict[str, Any] = await websocket.receive_json()
            last_seen["t"] = time.monotonic()
            op = msg.get("op")
            if not bucket.take():
                metrics.RATE_LIMITED.labels(scope="ws").inc()
                await websocket.send_json({"op": "error", "error": "slow down"})
                continue
            if op == "ping":
                await presence()
                await websocket.send_json({"op": "pong", "t": msg.get("t")})
            elif op == "resume":
                cursor["next"] = max(0, int(msg.get("last_seq", -1)) + 1)
                async with a.bell:
                    a.bell.notify_all()
            elif op == "act":
                if seat is None:
                    await websocket.send_json({"op": "ack", "request_id": msg.get("request_id"),
                                               "result": {"status": "forbidden"}})
                    continue
                res = await a.submit(seat, int(msg["seq"]), msg["action"], msg.get("request_id"))
                await websocket.send_json({"op": "ack", "request_id": msg.get("request_id"), "result": res})

    await presence()
    tasks = [asyncio.create_task(pusher()), asyncio.create_task(reader())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        metrics.WS_CONNECTIONS.dec()


# `uvicorn werewolf_harness.app.api.main:app` -- nothing connects until the lifespan runs
app = create_app()
