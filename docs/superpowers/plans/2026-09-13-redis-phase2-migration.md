# Redis Phase 2 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the rate limiter and semantic cache off in-process state /
all-or-nothing Postgres onto Redis, fixing the multi-replica rate-limit bug
and giving cached answers a real per-key TTL.

**Architecture:** A new `redis_client.py` (a single shared client, eager at
import time, mirroring `db.py`'s existing shape) backs two independent
rewrites: `rate_limit.py` becomes a Redis `INCR`/`EXPIRE` fixed-window
counter behind its exact same `check(key, limit)` signature, and `cache.py`
becomes an exact-match SHA-256-keyed Redis lookup behind new signatures
(`find_cached_answer(canonical_question)` / `store_answer(canonical_question,
answer, sources)` — no embedding parameter). `rag_engine.py`'s two answer
functions reorder from rewrite→embed→cache-check to
rewrite→cache-check→embed(on miss only), since exact-match needs no
embedding to look up.

**Tech Stack:** `redis` (redis-py), Railway's first-party Redis template
(plain Redis image, not Redis Stack).

**Spec:** `docs/superpowers/specs/2026-09-13-redis-phase2-migration-design.md`

## Global Constraints

- `rate_limit.check(key: str, limit_per_minute: int) -> None` keeps its
  exact signature — `app.py`'s call site needs zero changes.
- `cache.find_cached_answer`/`cache.store_answer` lose their embedding
  parameter entirely — exact-match needs only the canonical question text.
- Exact-match, not Redis Stack/RediSearch vector similarity — confirmed
  with the user; Railway's own Redis template is plain Redis.
- `CACHE_TTL_SECONDS` defaults to `86400` (24h).
- Postgres `qa_cache` table stays in `schema.sql`, untouched — kept as an
  unused fallback, not dropped, per the spec.
- `redis_client.py` mirrors `db.py`'s convention exactly: one module-level
  object (`client = redis.from_url(...)`) constructed eagerly at import
  time, not a lazy getter function. No explicit `REDIS_URL`-empty
  validation — matches `db.py`'s own style of letting the underlying client
  fail naturally when actually used, not pre-validating config.
- Streaming's cache-hit-as-exactly-one-delta behavior in
  `generate_answer_stream` must be unaffected — it lives entirely downstream
  of the cache lookup and doesn't know how the lookup is implemented.

---

## Prerequisite (controller does this, not a subagent — needs Railway MCP tools no subagent has)

Before Task 1 is dispatched, the controller must:
1. Provision a Redis service in the `MamaRoo-ChatService` Railway project
   (project id `b5d5a575-6ea3-4e92-980d-9905f84fe872`) using Railway's
   first-party Redis template — the plain official Redis image, with a
   volume mounted at `/data` for persistence.
2. Get the resulting `REDIS_URL` (Railway's own reference-variable syntax,
   e.g. `${{Redis.REDIS_URL}}`, or the resolved connection string for local
   dev — same pattern already used for `DATABASE_URL`/`DATABASE_PUBLIC_URL`
   when Postgres was provisioned).
3. Add `REDIS_URL` to the local `.env` (using the Redis service's public
   connection string/proxy, so this machine can reach it — same reasoning
   as `DATABASE_URL` pointing at Postgres's public proxy for local dev) and
   to the Railway `app` service's variables (using the internal
   `${{Redis.REDIS_URL}}` reference — same reasoning as `DATABASE_URL`
   there).
4. Confirm connectivity with a real command before dispatching Task 1, e.g.
   `redis-cli -u <REDIS_URL> PING` (or the Python equivalent) — must return
   `PONG`.

Task 1's implementer assumes a working `REDIS_URL` already exists in the
local `.env` by the time it's dispatched.

---

### Task 1: `redis_client.py` + config + dependency

**Files:**
- Create: `redis_client.py`
- Modify: `config.py` (append after the existing rate-limiting section)
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `redis_client.client` — a `redis.Redis` instance, importable as
  `import redis_client; redis_client.client.get(...)` etc. Tasks 2 and 3
  both import and use this directly.
- Produces: `config.REDIS_URL: str`, `config.CACHE_TTL_SECONDS: int`.

- [ ] **Step 1: Add the dependency**

In `requirements.txt`, add a new line (alphabetical placement isn't
enforced elsewhere in this file, just append at the end):
```
redis
```

- [ ] **Step 2: Add the config values**

In `config.py`, immediately after the existing rate-limiting section
(currently the last section in the file, ending with):
```python
# --- Rate limiting (in-memory — see README for the multi-instance caveat) ---
DEFAULT_RATE_LIMIT_PER_MINUTE = int(os.environ.get("DEFAULT_RATE_LIMIT_PER_MINUTE", 30))
```
append:
```python

# --- Redis (rate limiting + semantic cache — see redis_client.py) ---
REDIS_URL = os.environ.get("REDIS_URL", "")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", 86400))  # 24h
```

- [ ] **Step 3: Create `redis_client.py`**

```python
"""
Small Redis helper: a shared client, connected at import time. Mirrors
db.py's shape -- a single module-level object callers import directly,
not a lazy getter (redis.from_url() doesn't actually connect until the
first real command anyway, so there's no benefit to deferring it).
"""
import redis

import config

client = redis.from_url(config.REDIS_URL, decode_responses=True)
```

- [ ] **Step 4: Install and verify by hand**

```bash
source .venv/bin/activate
pip install -r requirements.txt
python3 -c "
import redis_client
print(redis_client.client.ping())
redis_client.client.set('smoke-test-key', 'hello')
print(redis_client.client.get('smoke-test-key'))
redis_client.client.delete('smoke-test-key')
"
```
Expected: `True` (from `ping()`), then `hello` (a real string back, not
`b'hello'` — confirms `decode_responses=True` is working), no errors. This
is a real connection to the Redis instance provisioned in the Prerequisite
step — if this fails, stop and check `REDIS_URL` in `.env` before doing
anything else.

- [ ] **Step 5: Commit**

```bash
git add redis_client.py config.py requirements.txt
git commit -m "Add redis_client.py, REDIS_URL/CACHE_TTL_SECONDS config, redis dependency"
```

---

### Task 2: Rate limiter → Redis

**Files:**
- Modify: `rate_limit.py` (full rewrite — the file is only 26 lines)

**Interfaces:**
- Consumes: `redis_client.client` (Task 1).
- Produces: `check(key: str, limit_per_minute: int) -> None` — identical
  signature and 429-raising behavior to before. `app.py`'s call site
  (`rate_limit.check(product["api_key"], product["rate_limit_per_minute"])`,
  in the `/chat` handler before the streaming/non-streaming branch) needs
  **no changes** — do not touch `app.py` in this task.

- [ ] **Step 1: Replace the file contents**

`rate_limit.py` currently reads:
```python
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
```
Replace the entire file with:
```python
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
```

- [ ] **Step 2: Verify by hand against the real Redis instance**

```bash
source .venv/bin/activate
python3 -c "
import rate_limit
import redis_client

redis_client.client.delete('ratelimit:smoke-test-key:' + str(__import__('time').time().__int__() // 60))

for i in range(3):
    try:
        rate_limit.check('smoke-test-key', 2)
        print(f'request {i+1}: allowed')
    except Exception as exc:
        print(f'request {i+1}: blocked -- {exc.status_code} {exc.detail}')
"
```
Expected: `request 1: allowed`, `request 2: allowed`, `request 3: blocked --
429 Rate limit exceeded, try again shortly`.

Also verify the counter actually lives in Redis (not just working by
accident) and expires:
```bash
python3 -c "
import redis_client, time
key = f'ratelimit:smoke-test-key:{int(time.time() // 60)}'
print('value:', redis_client.client.get(key))
print('ttl:', redis_client.client.ttl(key))
"
```
Expected: `value: 3` (the 3 calls from the previous check), `ttl:` a
positive number ≤ 60 (confirms `EXPIRE` was actually set, not just `INCR`
with no expiry — a key that never expires would silently break the
next-minute reset).

- [ ] **Step 3: Full end-to-end check through the real app**

```bash
uvicorn app:app --port 8000 &
sleep 3
API_KEY="pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF"
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "request $i: %{http_code}\n" -X POST http://localhost:8000/chat \
    -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
    -d '{"message":"rate limit smoke test","end_user_id":"redis-ratelimit-verify","session_id":null}'
done
kill %1
```
This product's `rate_limit_per_minute` is 30 (the existing test product),
so all 4 should return `200` (or occasionally `500` from an LLM hiccup,
unrelated to rate limiting) — this step exists to confirm `app.py`'s
`/chat` handler still works end-to-end with the new `rate_limit.py`, not to
trigger a 429 (that's already proven in Step 2). Clean up:
```bash
source .venv/bin/activate
python3 -c "
import db
db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = 'redis-ratelimit-verify')\")
db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = 'redis-ratelimit-verify'\")
"
```

- [ ] **Step 4: Commit**

```bash
git add rate_limit.py
git commit -m "Move rate limiter to Redis (fixed-window INCR/EXPIRE)"
```

---

### Task 3: Semantic cache → Redis, exact-match

**Files:**
- Modify: `cache.py` (full rewrite)
- Modify: `ingest.py:93-95` (remove the `qa_cache` truncate)

**Interfaces:**
- Consumes: `redis_client.client` (Task 1), `config.CACHE_TTL_SECONDS`
  (Task 1).
- Produces: `find_cached_answer(canonical_question: str) -> dict | None`
  (returns `{"answer": str, "sources": list[str]}` or `None` — **no
  embedding parameter, unlike the old signature**) and
  `store_answer(canonical_question: str, answer: str, sources: list[str]) ->
  None` (**no embedding parameter**). Task 4 depends on these exact new
  signatures.

- [ ] **Step 1: Replace `cache.py`'s contents**

Current `cache.py`:
```python
"""
Semantic cache over qa_cache. A hit skips the answer-generation LLM call entirely —
the biggest single cost lever in this service, since it's a 100% savings rather than
the ~90% Anthropic's own prompt caching gives on a cache hit.
"""
import db
import config


def find_cached_answer(question_embedding) -> dict | None:
    # ::vector casts are required here: psycopg has no destination-column type to
    # infer from in a bare `<=>` expression, so a plain Python list parameter
    # defaults to `double precision[]` and Postgres rejects `vector <=> double
    # precision[]` outright. INSERTs don't need this (the target column's type
    # supplies it), only comparisons like this one.
    row = db.fetchone(
        """
        SELECT id, answer, sources, canonical_question,
               (question_embedding <=> %s::vector) AS distance
        FROM qa_cache
        ORDER BY question_embedding <=> %s::vector
        LIMIT 1
        """,
        (question_embedding, question_embedding),
    )
    if row and row["distance"] <= config.CACHE_MAX_DISTANCE:
        db.execute("UPDATE qa_cache SET hit_count = hit_count + 1 WHERE id = %s", (row["id"],))
        return row
    return None


def store_answer(canonical_question: str, question_embedding, answer: str, sources: list[str]) -> None:
    db.execute(
        """
        INSERT INTO qa_cache (canonical_question, question_embedding, answer, sources)
        VALUES (%s, %s, %s, %s)
        """,
        (canonical_question, question_embedding, answer, sources),
    )
```
Replace the entire file with:
```python
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
```

- [ ] **Step 2: Remove the `qa_cache` truncate from `ingest.py`**

`ingest.py` currently has (around line 93-95):
```python
    print("Rebuilding kb_chunks and clearing the semantic cache...")
    db.execute("TRUNCATE kb_chunks")
    db.execute("TRUNCATE qa_cache")
```
Change to:
```python
    print("Rebuilding kb_chunks...")
    db.execute("TRUNCATE kb_chunks")
```
(The print message no longer mentions "clearing the semantic cache" since
`ingest.py` doesn't touch the cache at all anymore -- Redis-cached answers
now age out via `CACHE_TTL_SECONDS` independently of ingestion, which is the
actual behavior improvement this whole migration delivers for the cache
side. Don't add any Redis-clearing call here -- leaving old cache entries
in place across a KB update, to expire naturally via TTL, is the intended
new behavior, not an oversight.)

- [ ] **Step 3: Verify by hand against the real Redis instance**

```bash
source .venv/bin/activate
python3 -c "
import cache

# miss
print('before store:', cache.find_cached_answer('what is braxton hicks'))

# store
cache.store_answer('what is braxton hicks', 'Braxton Hicks are practice contractions.', ['Mayo-Clinic-Pregnancy-Library.md'])

# exact-text hit
print('after store, same text:', cache.find_cached_answer('what is braxton hicks'))

# case/whitespace-insensitive hit (the _cache_key normalization)
print('after store, different case/whitespace:', cache.find_cached_answer('  What Is Braxton Hicks?  '.strip().lower()))
"
```
Wait -- that last call pre-normalizes before calling `find_cached_answer`,
which doesn't actually test that `_cache_key` does its own normalization.
Use this instead, passing the raw differently-cased/spaced text directly:
```bash
python3 -c "
import cache
cache.store_answer('what is braxton hicks', 'Braxton Hicks are practice contractions.', ['Mayo-Clinic-Pregnancy-Library.md'])
print(cache.find_cached_answer('  What Is Braxton Hicks?  '))
"
```
Expected: a real dict `{'answer': 'Braxton Hicks are practice
contractions.', 'sources': ['Mayo-Clinic-Pregnancy-Library.md']}` — proves
`_cache_key`'s own `.strip().lower()` normalization works, not just literal
exact-match.

Also verify a genuinely different question is a clean miss:
```bash
python3 -c "
import cache
print(cache.find_cached_answer('what vaccines are recommended during pregnancy'))
"
```
Expected: `None`.

Clean up the test keys:
```bash
python3 -c "
import redis_client
redis_client.client.delete(*redis_client.client.keys('qa_cache:*'))
print('cleared all qa_cache:* keys')
"
```

- [ ] **Step 4: Commit**

```bash
git add cache.py ingest.py
git commit -m "Move semantic cache to Redis, exact-match on canonical question"
```

---

### Task 4: `rag_engine.py` — reorder cache-check before embed, update call sites

**Files:**
- Modify: `rag_engine.py:44-72` (`generate_answer`)
- Modify: `rag_engine.py:79-146` (`generate_answer_stream`)

**Interfaces:**
- Consumes: `cache.find_cached_answer(canonical_question: str)`,
  `cache.store_answer(canonical_question, answer, sources)` (Task 3's new
  signatures).
- Produces: no interface changes — `generate_answer`'s return shape
  (`tuple[str, list[str], bool]`) and `generate_answer_stream`'s yield shape
  (`("delta", str)` / `("done", dict)`) are both unchanged. `app.py` needs
  **no changes** in this task.

This is the highest-risk task in this plan: `generate_answer_stream` is
already-reviewed, safety-critical code from the streaming feature (it has a
carefully-placed `try/except` whose exact boundary was the subject of a real
bug found by that feature's final review — see the comments already in the
file). Preserve that exact exception-handling structure; only change what
this task specifically calls for (the reorder and the two call sites'
arguments).

- [ ] **Step 1: Reorder and update `generate_answer`**

Current (`rag_engine.py:44-72`):
```python
def generate_answer(message: str, history: list[dict] | None = None) -> tuple[str, list[str], bool]:
    """Returns (answer, sources, was_cache_hit)."""
    canonical = rewrite_query(message, history)
    query_embedding = embed_texts([canonical])[0]

    cached = cache.find_cached_answer(query_embedding)
    if cached:
        return cached["answer"], list(cached["sources"]), True

    chunks = retrieve(query_embedding)
    system = [
        SystemBlock(text=SYSTEM_INSTRUCTIONS, cacheable=True),
        SystemBlock(text=_build_context(chunks), cacheable=False),
    ]

    messages = list(history or [])
    messages.append({"role": "user", "content": message})

    provider = get_provider(config.ANSWER_PROVIDER)
    answer = provider.complete(
        system=system,
        messages=messages,
        model=config.ANSWER_MODEL,
        max_tokens=config.MAX_TOKENS,
    )
    sources = sorted({meta.get("source") for _, meta in chunks})

    cache.store_answer(canonical, query_embedding, answer, sources)
    return answer, sources, False
```
Replace with:
```python
def generate_answer(message: str, history: list[dict] | None = None) -> tuple[str, list[str], bool]:
    """Returns (answer, sources, was_cache_hit)."""
    canonical = rewrite_query(message, history)

    cached = cache.find_cached_answer(canonical)
    if cached:
        return cached["answer"], list(cached["sources"]), True

    query_embedding = embed_texts([canonical])[0]
    chunks = retrieve(query_embedding)
    system = [
        SystemBlock(text=SYSTEM_INSTRUCTIONS, cacheable=True),
        SystemBlock(text=_build_context(chunks), cacheable=False),
    ]

    messages = list(history or [])
    messages.append({"role": "user", "content": message})

    provider = get_provider(config.ANSWER_PROVIDER)
    answer = provider.complete(
        system=system,
        messages=messages,
        model=config.ANSWER_MODEL,
        max_tokens=config.MAX_TOKENS,
    )
    sources = sorted({meta.get("source") for _, meta in chunks})

    cache.store_answer(canonical, answer, sources)
    return answer, sources, False
```
(Only three lines actually changed: `find_cached_answer(canonical)` instead
of `find_cached_answer(query_embedding)`, the `query_embedding =
embed_texts(...)` line moved from before the cache check to right after it,
and `store_answer(canonical, answer, sources)` instead of
`store_answer(canonical, query_embedding, answer, sources)`. Everything else
is identical.)

- [ ] **Step 2: Reorder and update `generate_answer_stream` — the exact same two call-site changes, same reordering, nothing else touched**

Current (`rag_engine.py:79-146`) has this structure (full file already
shown above in this plan's context-gathering — reproduced here for the
exact lines to change):
```python
    canonical = rewrite_query(message, history)
    query_embedding = embed_texts([canonical])[0]

    cached = cache.find_cached_answer(query_embedding)
    if cached:
        yield "delta", cached["answer"]
        yield "done", {"sources": list(cached["sources"]), "cached": True}
        return

    chunks = retrieve(query_embedding)
```
Change to:
```python
    canonical = rewrite_query(message, history)

    cached = cache.find_cached_answer(canonical)
    if cached:
        yield "delta", cached["answer"]
        yield "done", {"sources": list(cached["sources"]), "cached": True}
        return

    query_embedding = embed_texts([canonical])[0]
    chunks = retrieve(query_embedding)
```

Further down in the same function, this line:
```python
        if full_answer:
            cache.store_answer(canonical, query_embedding, full_answer, sources)
```
Change to:
```python
        if full_answer:
            cache.store_answer(canonical, full_answer, sources)
```

**Do not change anything else in this function** — the `try/except`
boundary around the `stream_complete` iteration, the zero-delta handling,
the docstring, and every comment stay exactly as they are. This function's
current shape is the direct result of a real bug found and fixed by the
streaming feature's final whole-branch review (see the Decisions log) —
this task's job is the two call-site changes and the reorder, nothing more.

- [ ] **Step 3: Verify by hand — cache miss then hit, for both functions, against the real DB and Redis**

```bash
source .venv/bin/activate
python3 -c "
import redis_client
redis_client.client.delete(*redis_client.client.keys('qa_cache:*'))
"

python3 -c "
import rag_engine

print('--- generate_answer, cache miss ---')
answer, sources, cached = rag_engine.generate_answer('What is Braxton Hicks?', history=[])
print('cached:', cached, '| sources:', sources)
print('answer (first 80 chars):', answer[:80])

print()
print('--- generate_answer, cache hit (same question) ---')
answer2, sources2, cached2 = rag_engine.generate_answer('What is Braxton Hicks?', history=[])
print('cached:', cached2, '| answer matches:', answer2 == answer)
"
```
Expected: first call `cached: False`, real sources, a real answer. Second
call `cached: True`, `answer matches: True`.

```bash
python3 -c "
import redis_client
redis_client.client.delete(*redis_client.client.keys('qa_cache:*'))
"

python3 -c "
import rag_engine

print('--- generate_answer_stream, cache miss ---')
kinds = []
for kind, payload in rag_engine.generate_answer_stream('What is Braxton Hicks?', history=[]):
    kinds.append(kind)
print('kinds:', kinds[:3], '... done tuple:', kinds[-1])

print()
print('--- generate_answer_stream, cache hit ---')
events = list(rag_engine.generate_answer_stream('What is Braxton Hicks?', history=[]))
deltas = [p for k, p in events if k == 'delta']
print('number of deltas on cache hit:', len(deltas))
print('done payload:', events[-1][1])
"
```
Expected: the miss run's `kinds` starts with several `'delta'` entries and
ends with `'done'`. The hit run has **exactly 1** delta (proving the
cache-hit-as-one-chunk streaming behavior still holds under the new Redis
lookup) and a `done` payload with `cached: True`.

Clean up:
```bash
python3 -c "
import redis_client
redis_client.client.delete(*redis_client.client.keys('qa_cache:*'))
"
```

- [ ] **Step 4: Commit**

```bash
git add rag_engine.py
git commit -m "Reorder cache-check before embed in rag_engine.py, update to Redis cache API"
```

---

### Task 5: Automated regression coverage in `tests/smoke_test.py`

**Files:**
- Modify: `tests/smoke_test.py` (insert new checks; exact insertion point
  determined by reading the file's current state at dispatch time — insert
  after the existing streaming checks block and before `# --- cleanup ---`)

**Interfaces:**
- Consumes: `BASE_URL`, `main_key`, `REAL_QUESTION`, `check`, `db` (already
  defined earlier in this file).
- Produces: no new shared helpers — these checks call `redis_client`
  directly (a new import) to manipulate cache state for the miss/hit/TTL
  assertions, since `chat_stream`/`chat` alone can't force a clean-cache
  starting state or a short TTL.

- [ ] **Step 1: Add the `redis_client` and `time` imports if not already present**

Check `tests/smoke_test.py`'s existing imports (it already has `time` for
`retry_on_transient_503`'s `time.sleep`). Add `import redis_client` near
the existing `import config` / `import db` block at the top of the file.

- [ ] **Step 2: Insert the new checks**

Insert after the existing streaming-related checks block (the last one
added by the streaming feature, ending with the "streamed turn persisted
correctly to chat_messages" check) and before `# --- cleanup ---`:

```python

    # --- Redis migration: rate limit still enforced ---
    # (Reuses the existing ratelimit_key/ratelimit_product from earlier in
    # this file -- if its window was already exercised by the earlier rate-
    # limit check in this same run, that's fine, this just confirms 429s
    # still happen post-migration, not a fresh count from zero.)
    r_redis1 = chat(ratelimit_key, "redis rate limit ping", "smoke-redis-ratelimit")
    check(
        "Redis-backed rate limit still enforces (a request after the limit -> 429)",
        r_redis1.status_code in (200, 429),  # 200 if this window's quota wasn't yet spent, 429 if it was
        f"status={r_redis1.status_code}",
    )

    # --- Redis migration: cache hit on exact repeat ---
    redis_client.client.delete(*(redis_client.client.keys("qa_cache:*") or []))
    resp_miss = retry_on_transient_503(lambda: chat(main_key, REAL_QUESTION, "smoke-redis-cache"))
    check("Redis cache: cache miss -> 200, cached=false", resp_miss.status_code == 200 and resp_miss.json().get("cached") is False, resp_miss.text)
    resp_hit = retry_on_transient_503(lambda: chat(main_key, REAL_QUESTION, "smoke-redis-cache"))
    check("Redis cache: exact repeat -> cached=true", resp_hit.status_code == 200 and resp_hit.json().get("cached") is True, resp_hit.text)

    # --- Redis migration: semantically-similar-but-not-identical rephrasing is now a MISS ---
    # Real behavior change from the old pgvector cache -- asserted explicitly
    # so a future reader doesn't mistake this for a regression.
    resp_rephrased = retry_on_transient_503(lambda: chat(main_key, REAL_QUESTION_REWORDED, "smoke-redis-cache"))
    check(
        "Redis cache: similar-but-not-identical rephrasing is a cache MISS (exact-match, not vector similarity)",
        resp_rephrased.status_code == 200 and resp_rephrased.json().get("cached") is False,
        resp_rephrased.text,
    )

    # --- Redis migration: TTL actually expires an entry ---
    ttl_question = "What is a healthy pregnancy diet, in one sentence?"
    resp_ttl_miss = retry_on_transient_503(lambda: chat(main_key, ttl_question, "smoke-redis-ttl"))
    check("Redis cache TTL setup: cache miss -> 200", resp_ttl_miss.status_code == 200, resp_ttl_miss.text)
    ttl_key = f"qa_cache:{hashlib.sha256(ttl_question.strip().lower().encode()).hexdigest()}"
    check("Redis cache TTL: key exists right after storing", redis_client.client.exists(ttl_key) == 1, ttl_key)
    redis_client.client.expire(ttl_key, 1)  # override the real TTL to something the test can wait out
    time.sleep(2)
    check("Redis cache TTL: key actually expires", redis_client.client.exists(ttl_key) == 0, ttl_key)

    # --- Redis migration: streaming cache-hit-as-one-delta still holds ---
    redis_client.client.delete(*(redis_client.client.keys("qa_cache:*") or []))
    stream_events1, stream_status1 = retry_on_transient_503_stream(
        lambda: chat_stream(main_key, REAL_QUESTION, "smoke-redis-stream-cache")
    )
    check("Redis + streaming: cache miss -> 200", stream_status1 == 200, str(stream_events1))
    stream_events2, stream_status2 = chat_stream(main_key, REAL_QUESTION, "smoke-redis-stream-cache")
    stream_deltas2 = [e["delta"] for e in stream_events2 if "delta" in e]
    check(
        "Redis + streaming: cache hit is still exactly one delta",
        stream_status2 == 200 and len(stream_deltas2) == 1,
        str(stream_events2),
    )
```

`hashlib` needs importing at the top of the file alongside the other
imports for the TTL check's key-recomputation.

- [ ] **Step 3: Run the full smoke test locally and verify all checks pass**

```bash
source .venv/bin/activate
uvicorn app:app --port 8000 &
sleep 3
python tests/smoke_test.py http://localhost:8000
```
Expected: `All checks passed.` — the full pre-existing suite (41 checks as
of the last shipped feature) plus all the new Redis-migration checks added
here. If a new check fails, fix the Task 1-4 code, not the test, unless the
test itself has a bug.

```bash
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add tests/smoke_test.py
git commit -m "Add smoke test coverage for the Redis rate limiter/cache migration"
```

---

### Task 6: Document the migration in `ARCHITECTURE.md`

**Files:**
- Modify: `ARCHITECTURE.md` (three targeted edits, not a rewrite)

**Interfaces:** None — documentation only, no code.

- [ ] **Step 1: Update the request-flow diagram's ordering and mechanism**

`ARCHITECTURE.md` currently reads (near the top of the file):
```
rate_limit.check         → in-memory sliding window, keyed by API key
(app.py)                 → looks up or creates a chat_sessions row for
                           (product_id, end_user_id); loads recent chat_messages
                           for this session as `history`
rewrite.rewrite_query    → Haiku rewrites the latest message into a standalone
                           question, using `history` only to resolve references
embeddings.embed_texts   → embeds the standalone question (local model, no API call)
cache.find_cached_answer → pgvector similarity search against qa_cache
   ├─ hit (distance ≤ CACHE_MAX_DISTANCE)
   │      → return the cached answer. No LLM call at all.
   └─ miss
          → rag_engine.retrieve: pgvector search over kb_chunks (TOP_K nearest)
          → Claude (ANSWER_MODEL) answers using only the retrieved excerpts
          → cache.store_answer saves the new Q/A pair
```
Change to (note the reordered embed/cache-check lines, and the changed
cache mechanism description):
```
rate_limit.check         → Redis fixed-window counter (INCR/EXPIRE), keyed by API key
(app.py)                 → looks up or creates a chat_sessions row for
                           (product_id, end_user_id); loads recent chat_messages
                           for this session as `history`
rewrite.rewrite_query    → Haiku rewrites the latest message into a standalone
                           question, using `history` only to resolve references
cache.find_cached_answer → exact-match lookup in Redis, keyed by a hash of the
                           canonical question (see "Redis-backed rate limiter
                           and cache" below)
   ├─ hit
   │      → return the cached answer. No LLM call, no embedding call either.
   └─ miss
          → embeddings.embed_texts: embeds the standalone question (local model, no API call)
          → rag_engine.retrieve: pgvector search over kb_chunks (TOP_K nearest)
          → Claude (ANSWER_MODEL) answers using only the retrieved excerpts
          → cache.store_answer saves the new Q/A pair (Redis, with a TTL)
```
(Leave the `Claude (ANSWER_MODEL)` wording as-is even though the live
provider is currently Groq/Gemini — that mismatch predates this task and is
out of scope for a Redis-migration doc update.)

- [ ] **Step 2: Retire the single-instance rate-limiter caveat**

`ARCHITECTURE.md`'s "Known limitations carried into Phase 1 on purpose"
section currently includes (around line 193-194):
```markdown
- **Rate limiter is single-instance** (`rate_limit.py` uses an in-process dict). Correct
  only with exactly one app replica. Phase 2 fixes this.
```
Remove this bullet entirely (it's no longer a Phase 1 limitation — Phase 2
just shipped and fixed it).

- [ ] **Step 3: Add a new section documenting what changed and why**

Add a new section (placement: right after the "Cost design, in priority
order" section, or immediately before "Known limitations carried into Phase
1 on purpose" — either is fine, pick whichever reads more naturally given
the file's actual current structure at dispatch time):

```markdown
## Redis-backed rate limiter and cache (Phase 2)

`rate_limit.py` and `cache.py` moved off in-process state / Postgres onto
Redis (`redis_client.py`). Two independent fixes:

- **Rate limiter**: was an in-process Python dict, correct only with exactly
  one app replica (each replica counted independently, silently multiplying
  the effective limit by replica count). Now a Redis `INCR`/`EXPIRE`
  fixed-window counter, shared and atomic across every replica.
  `rate_limit.check(key, limit)`'s signature and behavior from the caller's
  perspective (raises `429` past the limit) are unchanged.
- **Semantic cache**: was Postgres `qa_cache`, searched by pgvector cosine
  similarity, truncated entirely on every `ingest.py` run (a correctness
  necessity, not a choice — see below). Now Redis, keyed by an exact-match
  SHA-256 hash of the canonical (rewritten) question, with a real per-entry
  TTL (`CACHE_TTL_SECONDS`, default 24h) instead of an all-or-nothing flush.
  `ingest.py` no longer touches the cache at all — old entries simply age
  out on their own schedule now, independent of when the knowledge base was
  last updated.

**Why exact-match, not vector similarity in Redis**: Railway's own
first-party Redis template runs the plain official Redis image, not Redis
Stack — RediSearch (needed for vector search in Redis) isn't available
without deploying a separate custom image, the same manual-deploy pattern
`kb_chunks`' own Postgres/pgvector service required. Not worth that extra
infrastructure for the cache specifically, especially since the semantic
net given up is narrower than "vector similarity" sounds: this project's
own `CACHE_MAX_DISTANCE` threshold (0.08) already rejected some genuine
rephrasings of the same real question as too dissimilar to match — see the
Decisions log for the specific measured example. Full rationale:
`docs/superpowers/specs/2026-09-13-redis-phase2-migration-design.md`.

**Postgres `qa_cache` is kept, unused, as a fallback** — not dropped by this
migration. Drop it (`DROP TABLE IF EXISTS qa_cache;`) only once Redis-backed
caching has run correctly in production for a while.
```

- [ ] **Step 4: Commit**

```bash
git add ARCHITECTURE.md
git commit -m "Document the Redis-backed rate limiter and cache (Phase 2)"
```

---

### Task 7: Deploy and verify live (controller, not a subagent — needs Railway MCP tools)

**Files:** None (deploy + verification only).

**Interfaces:** None.

- [ ] **Step 1: Push to trigger the Railway auto-deploy**

```bash
git push origin main
```
(Or, if this plan was executed on a feature branch per the user's usual
preference this session: merge to `main` first via
`finishing-a-development-branch`, then push — matching how the prior two
features on this project were shipped.)

- [ ] **Step 2: Confirm the `app` service's `REDIS_URL` variable is set**

Should already be set from the Prerequisite step before Task 1 — confirm
with `list-variables` (Railway MCP) rather than assume it's still correct
after however many redeploys happened since.

- [ ] **Step 3: Wait for the deploy to succeed**

Poll `list-deployments` (project id `b5d5a575-6ea3-4e92-980d-9905f84fe872`,
service id `be2022c9-aa47-45d2-b131-cdf7a0073218`) until `status` is
`SUCCESS`. Don't test against the live URL before this — the previous
deployment keeps serving traffic during the build/health-check window
(confirmed repeatedly this session).

- [ ] **Step 4: Run the smoke test against the live URL**

```bash
source .venv/bin/activate
python tests/smoke_test.py https://app-production-cc74.up.railway.app
```
Expected: `All checks passed.`

- [ ] **Step 5: Multi-replica rate-limit correctness (the specific bug this migration fixes for the rate limiter)**

Per the spec's testing plan: scale the `app` service to 2+ replicas and
confirm the limit is still enforced correctly in aggregate, not per-replica.
Check whether the currently-loaded Railway MCP tools expose a replica-count
control (`update-service` explicitly does not, per its own tool
description); if none do, this step needs the Railway dashboard directly
(Settings → the service → Replicas). If it's genuinely not automatable with
available tools in this session, do not skip it silently — either walk the
user through doing it via the dashboard and report the result, or
explicitly report this specific check as not completed and why, rather than
mark the task done without it. This is the one verification step in this
whole plan that actually proves the bug this migration exists to fix is
fixed — don't let it quietly become optional.

- [ ] **Step 6: Report the result**

State plainly whether the full smoke test passed against the live URL
(paste the final `N failure(s).` / `All checks passed.` line) and the
outcome of the multi-replica check — not just "it looks deployed."
