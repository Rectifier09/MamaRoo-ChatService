"""
Redis-backed fixed-window rate limiter, keyed by API key.

Uses INCR + EXPIRE on a key bucketed by the current calendar minute, so the
limit is enforced correctly no matter how many Railway replicas are running
-- each replica hits the same shared Redis counter instead of its own
in-process one (the bug this file's previous version had).

Accepted trade-off: this is a fixed window, not an exact sliding window, so
a client can in principle spend the full limit in the last second of one
window and again in the first second of the next (~2x burst right at a
minute boundary). Acceptable here because this limiter exists to cap
worst-case cost exposure from a leaked/scraped key (see ARCHITECTURE.md's
"Auth model" section), not to be a precise throttle -- Redis's atomic
INCR/EXPIRE across replicas is what actually matters, not the window shape.
"""
import time

from fastapi import HTTPException

import redis_client


def check(key: str, limit_per_minute: int) -> None:
    window_key = f"ratelimit:{key}:{int(time.time() // 60)}"
    count = redis_client.client.incr(window_key)
    if count == 1:
        redis_client.client.expire(window_key, 60)
    if count > limit_per_minute:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, try again shortly")
