"""
Simple in-process sliding-window rate limiter, keyed by API key.

This is intentionally not backed by Redis/Postgres to keep infra minimal — it only
enforces the limit correctly if the service runs as a single instance. If you scale
this to multiple Railway replicas, each replica gets its own independent counter
(so the effective limit becomes limit x replica_count) — move the counter into
Postgres or add Redis at that point.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException

_hits: dict[str, deque] = defaultdict(deque)


def check(key: str, limit_per_minute: int) -> None:
    now = time.time()
    window = _hits[key]
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= limit_per_minute:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, try again shortly")
    window.append(now)
