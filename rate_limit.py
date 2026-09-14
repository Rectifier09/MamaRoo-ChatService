"""
Redis-backed fixed-window rate limiter, keyed by a hash of the API key.

Uses INCR + EXPIRE (queued together in one atomic pipeline, EXPIRE with NX so
it only takes effect the first time the key is created) on a key bucketed by
the current calendar minute, so the limit is enforced correctly no matter how
many Railway replicas are running -- each replica hits the same shared Redis
counter instead of its own in-process one (the bug this file's previous
version had). Pipelining the two commands together (rather than issuing INCR,
inspecting the result, then conditionally issuing EXPIRE as a second round
trip) closes the window where a crash between the two calls would leave a key
with no TTL -- harmless in practice (the minute bucket rotates and the key is
never read again), but a real leak of keys that live forever otherwise.

The key name hashes the API key rather than embedding it directly, so the
raw, product-facing key doesn't end up sitting in Redis's on-disk snapshots
just as a side effect of rate limiting it.

Accepted trade-off: this is a fixed window, not an exact sliding window, so
a client can in principle spend the full limit in the last second of one
window and again in the first second of the next (~2x burst right at a
minute boundary). Acceptable here because this limiter exists to cap
worst-case cost exposure from a leaked/scraped key (see ARCHITECTURE.md's
"Auth model" section), not to be a precise throttle -- Redis's atomic
INCR/EXPIRE across replicas is what actually matters, not the window shape.

Fails OPEN on a Redis error: a connection failure or timeout logs a warning
and lets the request through rather than raising into an uncaught 500. Same
posture as the fixed-window trade-off above -- this limiter caps worst-case
abuse cost, it isn't a precise/security-critical throttle, so being briefly
uncapped during a Redis outage the caller has no way to know about is an
acceptable extension of that same trade-off, and it keeps the service
available rather than failing every request during an outage.
"""
import hashlib
import time

import redis
from fastapi import HTTPException

import redis_client


def check(key: str, limit_per_minute: int) -> None:
    key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
    window_key = f"ratelimit:{key_hash}:{int(time.time() // 60)}"
    try:
        pipe = redis_client.client.pipeline()
        pipe.incr(window_key)
        pipe.expire(window_key, 60, nx=True)
        count, _ = pipe.execute()
    except redis.RedisError as exc:
        print(f"[rate_limit] Redis error, failing open (request allowed): {exc}")
        return

    if count > limit_per_minute:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, try again shortly")
