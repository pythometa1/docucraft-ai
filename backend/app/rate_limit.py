"""Token-bucket rate limiting backed by Redis (spec §15.5) -- chosen over a
sliding-window counter because generation/ingestion traffic is naturally bursty
(a user uploading several source files, or stepping through the mapping wizard)
and a token bucket tolerates that burst up to its capacity while still capping
sustained rate.
"""

from fastapi import Depends

from app.config import settings
from app.models import User
from app.redis_client import redis_client
from app.security import error, get_current_user

# Atomic check-and-consume: refills `capacity` tokens over `refill_period` seconds,
# consumes 1 token per call, denies when the bucket is empty. Lua keeps the
# read-refill-write cycle atomic under concurrent requests from the same key.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local last_ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  last_ts = now
end

local elapsed = math.max(0, now - last_ts)
tokens = math.min(capacity, tokens + elapsed * refill_per_sec)

local allowed = 0
if tokens >= 1 then
  allowed = 1
  tokens = tokens - 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return allowed
"""

_script = redis_client.register_script(_TOKEN_BUCKET_LUA)


def check_rate_limit(key: str, capacity: int, refill_per_sec: float) -> bool:
    import time

    allowed = _script(keys=[key], args=[capacity, refill_per_sec, time.time(), 3600])
    return bool(int(allowed))


def rate_limit_by_org(user: User = Depends(get_current_user)) -> None:
    """Per-org API rate limit -- attach as a router-level dependency."""
    capacity = settings.rate_limit_burst
    refill_per_sec = settings.rate_limit_per_minute / 60.0
    if not check_rate_limit(f"ratelimit:org:{user.org_id}", capacity, refill_per_sec):
        raise error("RATE_LIMITED", "Too many requests -- slow down and try again shortly.", 429)


def rate_limit_by_ip(request) -> None:
    """Per-IP limiter for unauthenticated endpoints (e.g. login) to blunt brute force."""
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"ratelimit:ip:{client_ip}", capacity=10, refill_per_sec=10 / 60.0):
        raise error("RATE_LIMITED", "Too many attempts -- slow down and try again shortly.", 429)
