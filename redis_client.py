"""
Small Redis helper: a shared client, connected at import time. Mirrors
db.py's shape -- a single module-level object callers import directly,
not a lazy getter (redis.from_url() doesn't actually connect until the
first real command anyway, so there's no benefit to deferring it).
"""
import redis

import config

client = redis.from_url(config.REDIS_URL, decode_responses=True)
