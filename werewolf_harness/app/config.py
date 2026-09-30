"""One place that reads the environment. Everything else takes a Settings."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/wh"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_ttl_s: int = 7 * 24 * 3600
    seat_token_ttl_s: int = 6 * 3600
    node_id: str = "node-1"

    # runtime
    lease_ttl_ms: int = 10_000          # a node that stops renewing loses the game after this
    lease_renew_s: float = 3.0
    recovery_scan_s: float = 5.0        # how often a node looks for games nobody holds
    agent_timeout_s: float = 240.0      # a model turn that takes longer is replaced by the fallback
    human_turn_s: float = 300.0

    # model layer
    agent_executor: str = "local"       # local | streams
    agent_workers: int = 16
    daily_budget_usd: float = 5.0
    # tokens per minute per model, as the provider grants them; {"gpt-x": 800000, ...}
    llm_tpm: dict[str, int] = {}
    llm_default_tpm: int = 2_000_000

    # api
    rate_limit_per_min: int = 240       # per IP, all routes
    create_limit_per_hour: int = 30     # per user, POST /api/games
    heartbeat_s: float = 25.0           # < Cloudflare's 100 s idle cut, with room for two misses


@lru_cache
def get_settings() -> Settings:
    return Settings()
