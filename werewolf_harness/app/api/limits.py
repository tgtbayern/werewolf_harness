"""Rate limits in Redis.

Sliding window log (one ZSET per key): every request is a member scored by its
timestamp; drop members older than the window, count, admit if under the limit.
Exact -- no burst at a window boundary as a fixed-window counter allows -- at
the cost of O(limit) memory per key. One Lua script, so the check-and-add is
atomic across API processes.
"""
from __future__ import annotations

import time
import uuid

from redis.asyncio import Redis

_SLIDING = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local n = redis.call('ZCARD', KEYS[1])
if n >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, math.ceil(oldest[2] + window - now)}
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], window)
return {1, 0}
"""


class SlidingWindow:
    def __init__(self, redis: Redis):
        self.r = redis
        self._script = redis.register_script(_SLIDING)

    async def hit(self, key: str, limit: int, window_ms: int) -> tuple[bool, int]:
        """(allowed, retry_after_ms)."""
        now = int(time.time() * 1000)
        ok, retry = await self._script(keys=[f"rl:{key}"], args=[now, window_ms, limit, f"{now}-{uuid.uuid4().hex[:8]}"])
        return bool(ok), int(retry)


class TokenBucket:
    """In-process bucket for one WebSocket connection (messages per second)."""

    def __init__(self, rate: float, burst: int):
        self.rate, self.burst = rate, burst
        self.tokens = float(burst)
        self.at = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False
