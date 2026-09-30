"""The model-call layer every seat of every game goes through.

    GatewayClient.chat(...)            (same interface as any LLMClient)
      ├─ game budget      tokens spent by this game so far (Redis) >= its cap -> BudgetExhausted
      └─ for model in chain (primary, then fallbacks):
           ├─ breaker     provider's breaker open -> skip to the next model at once
           ├─ reserve     token bucket of that provider (Redis, shared by all workers)
           ├─ call        the real client
           ├─ classify    429 / timeout / 5xx / context too long / content refusal -> next model
           │              wallet or key refused -> raise (the game stops; retrying is pointless)
           │              other 4xx -> raise (our request is wrong; another model will not fix it)
           └─ settle      return unused reservation; add to the game's spend; record the call

Every attempt, successful or not, becomes one CallRecord (-> llm_calls table and
Prometheus). If every model in the chain fails, the error goes back to the seat's
own retry policy and, after that, to the rule-based seat (runtime/rules_npc.py):
a game never stalls because a provider is down.
"""
from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass

import redis as redis_sync

from ...players.budget import BudgetExhausted, estimate_messages
from ...players.providers.base import LLMClient, LLMResponse, ProviderError
from ...players.providers.mock import MockClient
from ...players.recovery import is_terminal_refusal
from ..obs import metrics
from . import pricing
from .breaker import Breaker
from .limits import GameBudget, TokenLimiter
from .sim import PrefixCache, SimClient


@dataclass
class CallRecord:
    game_id: str | None
    seat: int | None
    seq: int | None
    model: str
    provider: str
    status: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    fallback_reason: str | None = None

    def to_row(self) -> dict:
        return dict(self.__dict__)


class CallSink:
    """Thread-safe buffer; the runtime drains it into Postgres in batches."""

    def __init__(self, maxlen: int = 50_000):
        self._q: collections.deque[CallRecord] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def put(self, r: CallRecord) -> None:
        with self._lock:
            self._q.append(r)

    def drain(self, n: int = 1000) -> list[CallRecord]:
        with self._lock:
            out = []
            while self._q and len(out) < n:
                out.append(self._q.popleft())
            return out


def classify(exc: ProviderError) -> str | None:
    """Why a call failed, if another model might succeed; None if it would not."""
    msg = str(exc).lower()
    if exc.status == 429 or "rate limit" in msg:
        return "rate_limited"
    if exc.status is None and "timed out" in msg:
        return "timeout"
    if exc.status is None or (exc.status and exc.status >= 500):
        return "upstream_error"
    if exc.status == 400 and ("context" in msg or "too long" in msg or "maximum" in msg):
        return "context_length"
    if "content" in msg and ("policy" in msg or "filter" in msg or "safety" in msg) or "敏感" in msg:
        return "content_policy"
    return None


def cached_tokens_of(resp: LLMResponse) -> int:
    u = (resp.raw or {}).get("usage") or {}
    d = u.get("prompt_tokens_details") or {}
    return int(d.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or u.get("cache_read_input_tokens") or 0)


SIM_PROFILES = {
    "sim": {"median_ms": 800},
    "sim-strong": {"median_ms": 1500},
    "sim-cheap": {"median_ms": 300},
    "sim-flaky": {"median_ms": 800, "timeout_rate": 0.3, "error_rate": 0.2},
    "sim-down": {"median_ms": 0, "error_rate": 1.0},
    "sim-fast": {"median_ms": 0},
}


class Hub:
    """Per-process shared state of the model layer: limits, breakers, sink, caches."""

    def __init__(self, redis_url: str, *, env_keys: dict | None = None, tpm: dict[str, int] | None = None,
                 sink: CallSink | None = None, breaker_kw: dict | None = None, sim_sleep: bool = True,
                 limiter_wait_s: float = 10.0):
        self.r = redis_sync.Redis.from_url(redis_url)
        self.env_keys = env_keys or {}
        self.limiter = TokenLimiter(self.r, tpm or {})
        self.budget = GameBudget(self.r)
        self.sink = sink or CallSink()
        self.breaker_kw = breaker_kw or {}
        self._breakers: dict[str, Breaker] = {}
        self._lock = threading.Lock()
        self.sim_cache = PrefixCache()
        self.sim_sleep = sim_sleep
        self.limiter_wait_s = limiter_wait_s

    def breaker(self, name: str) -> Breaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = Breaker(name, **self.breaker_kw)
            return self._breakers[name]

    def raw_client(self, model: str, seed: int) -> LLMClient:
        if model == "mock":
            return MockClient(seed=seed)
        if model in SIM_PROFILES:
            return SimClient(seed, name=model, cache=self.sim_cache, sleep=self.sim_sleep, **SIM_PROFILES[model])
        from ...server.service import _client
        return _client(model, seed, self.env_keys)

    def client(self, model: str, seed: int, *, game_id: str | None, seat: int | None, seq: int | None,
               max_game_tokens: int | None, fallbacks: list[str] | None = None) -> GatewayClient:
        chain = [model] + [m for m in (fallbacks or []) if m != model]
        return GatewayClient(self, [(m, self.raw_client(m, seed)) for m in chain], game_id=game_id, seat=seat,
                             seq=seq, max_game_tokens=max_game_tokens)


class GatewayClient(LLMClient):
    tool_mode = "native"

    def __init__(self, hub: Hub, chain: list[tuple[str, LLMClient]], *, game_id, seat, seq, max_game_tokens):
        self.hub = hub
        self.chain = chain
        self.name = chain[0][0]
        self.tool_mode = getattr(chain[0][1], "tool_mode", "native")
        self.game_id, self.seat, self.seq = game_id, seat, seq
        self.max_game_tokens = max_game_tokens

    def _record(self, model: str, status: str, **kw) -> None:
        self.hub.sink.put(CallRecord(self.game_id, self.seat, self.seq, model, _provider(model), status, **kw))

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=800, timeout=30.0) -> LLMResponse:
        if self.game_id and self.max_game_tokens and self.hub.budget.spent(self.game_id) >= self.max_game_tokens:
            raise BudgetExhausted(f"game {self.game_id} spent its {self.max_game_tokens} tokens")
        est = estimate_messages(messages, tools)
        reason: str | None = None
        for i, (model, client) in enumerate(self.chain):
            br = self.hub.breaker(model)
            if not br.allow():
                reason = reason or "breaker_open"
                self._record(model, "rejected", fallback_reason="breaker_open")
                continue
            want = est + max_tokens
            if not self.hub.limiter.reserve(model, want, self.hub.limiter_wait_s):
                reason = reason or "rate_limited_local"
                self._record(model, "rejected", fallback_reason="rate_limited_local")
                continue
            t0 = time.monotonic()
            try:
                resp = client.chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
            except ProviderError as exc:
                ms = int((time.monotonic() - t0) * 1000)
                self.hub.limiter.settle(model, want, 0)
                if is_terminal_refusal(exc):
                    self._record(model, "error", latency_ms=ms, fallback_reason="terminal_refusal")
                    raise
                why = classify(exc)
                if why is None:
                    self._record(model, "error", latency_ms=ms, fallback_reason="client_error")
                    raise
                if why in ("timeout", "upstream_error", "rate_limited"):
                    br.record(False)
                    metrics.BREAKER_STATE.labels(provider=model).set(1 if br.state == "open" else 0)
                reason = reason or why
                self._record(model, "timeout" if why == "timeout" else "error", latency_ms=ms, fallback_reason=why)
                metrics.LLM_CALL_MS.labels(model=model, status="error").observe(ms)
                continue
            ms = int((time.monotonic() - t0) * 1000)
            br.record(True)
            metrics.BREAKER_STATE.labels(provider=model).set(1 if br.state == "open" else 0)
            cached = cached_tokens_of(resp)
            used = resp.prompt_tokens + resp.completion_tokens
            self.hub.limiter.settle(model, want, used)
            if self.game_id:
                self.hub.budget.add(self.game_id, used)
            usd = pricing.cost(model, resp.prompt_tokens, cached, resp.completion_tokens)
            fb = reason if i > 0 else None
            self._record(model, "ok", input_tokens=resp.prompt_tokens, cached_tokens=cached,
                         output_tokens=resp.completion_tokens, latency_ms=ms, cost_usd=usd, fallback_reason=fb)
            metrics.LLM_CALL_MS.labels(model=model, status="ok").observe(ms)
            metrics.LLM_TOKENS.labels(model=model, kind="input").inc(resp.prompt_tokens)
            metrics.LLM_TOKENS.labels(model=model, kind="cached").inc(cached)
            metrics.LLM_TOKENS.labels(model=model, kind="output").inc(resp.completion_tokens)
            metrics.LLM_COST.labels(model=model).inc(usd)
            if fb:
                metrics.LLM_FALLBACKS.labels(reason=fb).inc()
            return resp
        raise ProviderError(f"every model in the chain failed ({reason})", status=503)


def _provider(model: str) -> str:
    if model in ("mock", "script") or model.startswith("sim"):
        return "local"
    try:
        from ...players.providers import provider_of
        p = provider_of(model)
        return p.label if p else "relay"
    except Exception:                                    # noqa: BLE001
        return "relay"
