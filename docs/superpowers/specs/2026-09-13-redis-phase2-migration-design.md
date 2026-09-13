# Redis Phase 2 Migration — Design

**Status:** Approved (design confirmed in chat 2026-09-13). Not yet implemented.
**Author:** Claude (session with the project owner), 2026-09-13.
**Supersedes/refines:** `PHASE_2_MIGRATION.md` (the reference implementation's
own original Phase 2 doc) — that doc's core intent (what moves, what stays)
is preserved, but this spec makes concrete choices it deliberately left open,
and accounts for two things built after it was written: the Groq/Gemini
provider split and the `generate_answer_stream()` streaming path (both of
which call the semantic cache and must keep working correctly through this
migration).

## Problem

Two of this service's Phase 1 mechanisms are documented, accepted
limitations of running on Postgres/in-process state:

1. **Rate limiting** (`rate_limit.py`) is an in-process Python dict. Correct
   only with exactly one app replica — scale to N replicas and the effective
   limit silently becomes `limit × N`, since each replica counts
   independently.
2. **Semantic cache** (`qa_cache` in Postgres) is all-or-nothing: every
   `ingest.py` run truncates the entire cache, even though a content change
   usually only invalidates a few affected answers. There's no way for a
   cached answer to expire gracefully on its own.

This spec moves both onto Redis, fixing both limitations, without touching
`kb_chunks` (vector search stays on Postgres/pgvector) or anything else.

## Non-goals (explicitly out of scope for this spec)

- Migrating vector search (`kb_chunks`) to Redis — stays on Postgres/pgvector.
- Dropping the Postgres `qa_cache` table now — kept as an unused fallback
  until Redis-backed caching has run correctly for a while (see "Postgres
  `qa_cache` retained" below).
- Redis Stack / RediSearch vector-similarity caching — considered and
  explicitly not chosen; see "Decision: exact-match caching" below.
- Any change to `/chat`'s request/response contract, `/chat/sessions`, or
  streaming's SSE event shapes — this spec only changes what happens
  *inside* the rate-limit and cache-lookup steps, not what a client sees.
- Preserving `qa_cache.hit_count` — currently write-only telemetry (never
  read anywhere in the app); dropped rather than reimplemented in Redis.

## Decision: exact-match caching, not Redis Stack vector similarity

The original `PHASE_2_MIGRATION.md` offered two options: RediSearch vector
similarity (closest like-for-like replacement for today's pgvector
behavior) or exact-match on the canonical rewritten question (simpler, less
infra). **Chosen: exact-match**, confirmed directly with the user, for two
reasons:
1. Railway's own Redis template runs the plain official Redis image, not
   Redis Stack — RediSearch would need a different, custom-deployed image
   (same manual pattern used for Postgres/pgvector earlier), more
   infrastructure to stand up and maintain for a single-purpose cache.
2. The semantic net today is narrower than it sounds: this session's own
   testing found two genuine rephrasings of the same question ("What is
   Braxton Hicks?" vs. "Can you explain what Braxton Hicks contractions
   are?") measured a cosine distance of 0.159 — roughly double
   `CACHE_MAX_DISTANCE` (0.08) — meaning they would **not** have matched
   under the current pgvector semantic cache either. Exact-match on the
   canonical (already-rewritten, already-normalized) question is a smaller
   behavior change than it first appears.

## API design

No change to any HTTP endpoint. This spec is entirely internal.

## Rate limiter (`rate_limit.py`)

**Signature unchanged:** `check(key: str, limit_per_minute: int) -> None`
still raises `HTTPException(429, ...)` or returns `None`. `app.py`'s single
call site (`rate_limit.check(product["api_key"],
product["rate_limit_per_minute"])`, before the `/chat` handler branches into
streaming vs. non-streaming) needs zero changes.

**Internal implementation — fixed-window counter via Redis `INCR`/`EXPIRE`:**
```python
def check(key: str, limit_per_minute: int) -> None:
    window_key = f"ratelimit:{key}:{int(time.time() // 60)}"
    count = redis_client.client.incr(window_key)
    if count == 1:
        redis_client.client.expire(window_key, 60)
    if count > limit_per_minute:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, try again shortly")
```

**Accepted behavior change:** this is a fixed-window counter (bucketed by
calendar minute), not the current sliding-window (exact per-request
timestamps in a deque). A fixed window can allow a burst up to ~2× the
configured limit right at a minute boundary (e.g. the full limit spent in
the last second of one window, then again in the first second of the next).
Confirmed acceptable with the user: this limiter exists to cap worst-case
cost exposure from a leaked/scraped key, not to be a precise throttle — see
`ARCHITECTURE.md`'s "Auth model" section, which already frames the rate
limit this way. The atomicity of `INCR`+`EXPIRE` across replicas is what
actually matters here, and Redis gives that correctly regardless of window
shape.

## Semantic cache (`cache.py`)

**Signature changes** (this is the one real interface change in this spec):

```python
# Before (Postgres/pgvector):
def find_cached_answer(question_embedding) -> dict | None: ...
def store_answer(canonical_question, question_embedding, answer, sources) -> None: ...

# After (Redis, exact-match):
def find_cached_answer(canonical_question: str) -> dict | None: ...
def store_answer(canonical_question: str, answer: str, sources: list[str]) -> None: ...
```

No embedding parameter on either function — exact-match needs only the
canonical question text.

**Redis key design:**
```python
import hashlib

def _cache_key(canonical_question: str) -> str:
    digest = hashlib.sha256(canonical_question.strip().lower().encode()).hexdigest()
    return f"qa_cache:{digest}"
```
Lowercased + stripped before hashing so trivial whitespace/casing
differences in an otherwise-identical rewritten question still hit — this
is a cheap normalization exact-match can afford that the old pgvector
lookup didn't need (embeddings already absorbed that kind of noise).

**`find_cached_answer`:**
```python
def find_cached_answer(canonical_question: str) -> dict | None:
    raw = redis_client.client.get(_cache_key(canonical_question))
    if raw is None:
        return None
    data = json.loads(raw)
    return {"answer": data["answer"], "sources": data["sources"]}
```
Returns the same two-key shape (`answer`, `sources`) both call sites already
consume — `rag_engine.py` never read `id`/`distance`/`canonical_question`/
`hit_count` from the old Postgres row, so nothing downstream needs to change
beyond the call sites themselves (see below).

**`store_answer`:**
```python
def store_answer(canonical_question: str, answer: str, sources: list[str]) -> None:
    redis_client.client.setex(
        _cache_key(canonical_question),
        config.CACHE_TTL_SECONDS,
        json.dumps({"answer": answer, "sources": sources}),
    )
```
`SETEX` sets the value and TTL atomically in one call — no separate `EXPIRE`
needed (unlike the rate limiter, which must `INCR` before it knows the key
is new).

## `rag_engine.py` — call-site + flow-order changes

Both `generate_answer()` and `generate_answer_stream()` currently call
`embed_texts([canonical])[0]` **before** the cache check, because the old
`find_cached_answer` needed the embedding. Exact-match doesn't need an
embedding for the lookup at all, so both functions reorder to:

**rewrite → cache-check (by text) → (on miss) embed → retrieve → generate → store**

This is a genuine (small) efficiency win, not just a mechanical rename: a
cache hit now skips the local embedding computation too, not only the LLM
call. Concretely, in both functions:
- `cache.find_cached_answer(query_embedding)` → `cache.find_cached_answer(canonical)`
- The `embed_texts([canonical])[0]` line moves from before the cache check
  to after it (only runs on a miss, right before `retrieve(query_embedding)`
  — `retrieve` still needs a real embedding for the pgvector KB search,
  that part is completely unaffected by this spec).
- `cache.store_answer(canonical, query_embedding, answer, sources)` →
  `cache.store_answer(canonical, answer, sources)`.

The cache-hit branch's shape (`cached["answer"]`, `list(cached["sources"])`)
is unchanged in both functions — including streaming's "cache hit streams as
exactly one delta" behavior, which lives entirely downstream of the lookup
and doesn't know or care how the lookup was implemented.

## New file: `redis_client.py`

Mirrors `db.py`'s actual shape exactly: `db.py` constructs its
`ConnectionPool` once at module level (not behind a lazy getter) and callers
import the pool-backed helpers directly. `redis_client.py` does the same —
`redis.from_url(...)` doesn't connect until the first real command anyway,
so there's no benefit to lazy construction, and matching `db.py`'s existing
convention (rather than inventing a different lazy-singleton pattern) keeps
the two infra modules easy to read side by side:

```python
"""
Small Redis helper: a shared client, connected at import time. Mirrors
db.py's shape — a single module-level object callers import directly.
"""
import redis

import config

client = redis.from_url(config.REDIS_URL, decode_responses=True)
```

Callers use `redis_client.client` directly (`.incr()`, `.expire()`,
`.get()`, `.setex()`) — Redis's client API is already a thin, well-known
interface, unlike psycopg's cursor/connection dance that `db.py`'s
`fetchone`/`fetchall`/`execute` wrappers exist to simplify, so there's no
need to re-wrap individual Redis commands behind custom helper functions
here. `decode_responses=True` so callers get `str` back from Redis, not
`bytes` — matches how the rest of the codebase (Postgres via `dict_row`)
already hands back plain Python types, not driver-specific wrappers.

## `config.py` additions

```python
# --- Redis (rate limiting + semantic cache) ---
REDIS_URL = os.environ.get("REDIS_URL", "")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", 86400))  # 24h
```

## `requirements.txt` addition

`redis` (the `redis-py` package — no extras needed, plain client only).

## `ingest.py` change

Remove the `db.execute("TRUNCATE qa_cache")` line — nothing writes to that
Postgres table anymore, so there's nothing to clear on re-ingest. The
Postgres `TRUNCATE kb_chunks` line is unaffected (vector search stays on
Postgres). Redis-cached answers now age out via `CACHE_TTL_SECONDS`
regardless of ingestion — a real behavior change from Phase 1's
all-or-nothing flush, and the entire point of this migration for the cache
side.

## Postgres `qa_cache` retained, not dropped

The table and its index stay in `schema.sql` untouched by this spec. Once
Redis-backed caching has been running correctly in production for a while,
dropping it (`DROP TABLE IF EXISTS qa_cache;`) is a follow-up, not part of
this work — matches the original design doc's own stated reasoning
(de-risks the migration; avoids deleting the working Phase 1 mechanism
before its replacement is proven).

## Infra

New Railway service in the existing `MamaRoo-ChatService` project: Railway's
own first-party Redis template (the official Redis Docker image — confirmed
via Railway's docs this is plain Redis, not Redis Stack, which is exactly
why exact-match was chosen over RediSearch). Volume mounted at `/data` for
persistence across restarts/redeploys (per Railway's own "Configure Redis as
a persistent store" guidance). `REDIS_URL` wired to the `app` service via
Railway's reference-variable syntax, same pattern as `DATABASE_URL`.

## Testing plan

1. **Local, against the real dev Redis and Postgres** (same no-mocking
   pattern as every other feature this session):
   - Rate limiter: hit a low-limit test product's `/chat` repeatedly,
     confirm `429` after the configured limit within one window — same
     check `tests/smoke_test.py` already does, should need no test changes
     since the external behavior (`check(key, limit)` → `429` past the
     limit) is unchanged.
   - Cache: a cache-miss question stores an entry; the *exact same* question
     text again is a cache hit; a semantically-similar-but-not-identical
     rephrasing is now correctly a cache **miss** (this is the accepted
     behavior change from switching off pgvector similarity — worth an
     explicit test asserting this new behavior, not just the hit case, so a
     future reader doesn't mistake the miss for a bug).
   - Streaming: confirm `generate_answer_stream`'s cache-hit-as-one-delta
     behavior still holds with the new Redis-backed lookup.
   - TTL: confirm a cache entry actually expires after `CACHE_TTL_SECONDS`
     (a short-TTL override for the test, not waiting 24 real hours).
   - Confirm `ingest.py` no longer touches `qa_cache` and existing Redis
     cache entries survive a re-ingest (this is the actual fix this
     migration delivers for the cache side — must be proven, not assumed).
2. **Extend `tests/smoke_test.py`** with the above as automated checks.
3. **Multi-replica rate-limit correctness** — the specific failure mode this
   migration fixes for the rate limiter. Scale the Railway `app` service to
   2+ replicas and confirm the limit is still enforced correctly in
   aggregate, not per-replica — matches the original design doc's own
   verification requirement, not weakened by this spec.
4. **Live verification against the deployed Railway instance** after
   deploying, matching this project's established practice throughout this
   session.

## Files touched

| File | Change |
|---|---|
| `redis_client.py` | New — thin shared-client wrapper, mirrors `db.py` |
| `rate_limit.py` | Reimplemented against Redis; `check(key, limit)` signature unchanged |
| `cache.py` | Reimplemented against Redis, exact-match; `find_cached_answer`/`store_answer` signatures change (no embedding param) |
| `rag_engine.py` | Both `generate_answer`/`generate_answer_stream`: reorder rewrite→cache-check→embed (was rewrite→embed→cache-check); update the two call sites' arguments |
| `config.py` | Add `REDIS_URL`, `CACHE_TTL_SECONDS` |
| `requirements.txt` | Add `redis` |
| `ingest.py` | Remove `TRUNCATE qa_cache` line |
| `tests/smoke_test.py` | New checks per the Testing plan above |
| `ARCHITECTURE.md` | Document the Redis-backed rate limiter/cache, retire the "single-instance only" caveat |
