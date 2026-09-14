"""
Redis-backed semantic cache, exact-match on the canonical (rewritten)
question. A hit skips the answer-generation LLM call entirely -- the biggest
single cost lever in this service, since it's a 100% savings rather than the
~90% Anthropic's own prompt caching gives on a cache hit.

Exact-match, not vector similarity: Railway's own Redis template runs plain
Redis, not Redis Stack, so RediSearch vector search isn't available without
deploying a separate custom image -- not worth the extra infrastructure for
this. See docs/superpowers/specs/2026-09-13-redis-phase2-migration-design.md
for the full rationale, including why the semantic net this gives up is
narrower than it sounds (two genuine rephrasings of the same real question
measured well outside the old pgvector cache's own similarity threshold).
"""
import hashlib
import json

import config
import redis_client


def _cache_key(canonical_question: str) -> str:
    digest = hashlib.sha256(canonical_question.strip().lower().encode()).hexdigest()
    return f"qa_cache:{digest}"


def find_cached_answer(canonical_question: str) -> dict | None:
    raw = redis_client.client.get(_cache_key(canonical_question))
    if raw is None:
        return None
    data = json.loads(raw)
    return {"answer": data["answer"], "sources": data["sources"]}


def store_answer(canonical_question: str, answer: str, sources: list[str]) -> None:
    redis_client.client.setex(
        _cache_key(canonical_question),
        config.CACHE_TTL_SECONDS,
        json.dumps({"answer": answer, "sources": sources}),
    )
