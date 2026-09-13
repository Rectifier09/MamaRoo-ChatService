# Phase 2 — Adding Redis

**Don't start this until Phase 1's definition of done (in `BUILD_PLAN.md`) is met and
verified in production on Railway.** This phase is additive — nothing from Phase 1
gets torn out or redesigned, two things just move where they live.

## What's moving, and what isn't

| Stays in Postgres | Moves to Redis |
|---|---|
| `kb_chunks` (vector search) | Semantic cache (`qa_cache` → Redis) |
| `chat_sessions`, `chat_messages` | Rate limiting (`rate_limit.py`'s in-memory dict → Redis) |
| `products` | |

Why these two and not the others: both are the parts of Phase 1 explicitly called out
as "correct only with a single instance" or "correct only until the next full
re-ingest." Redis fixes both, and neither chat history nor the product registry nor
the vector index have that problem — they don't need to move.

## Why this fixes the Phase 1 limitations

- **Rate limiting** — Redis's `INCR` + `EXPIRE` are atomic across processes, so the
  limit is correct regardless of how many Railway replicas are running. Phase 1's
  in-memory dict is per-process and silently multiplies the effective limit by
  replica count.
- **Semantic cache** — Redis gives cheap per-key TTLs, so cached answers can expire
  gradually (e.g., 24-72 hours) instead of Phase 1's all-or-nothing flush on every
  `ingest.py` run. This matters more as the knowledge base gets updated more often.

## Infra change

Add one Redis instance to the Railway project — either Railway's own Redis plugin, or
an external provider like Upstash if you'd rather not run persistent Redis yourself.
Either way, you'll get a `REDIS_URL` to add to the app service's variables.

## Code changes expected

1. **`config.py`** — add `REDIS_URL` (required in Phase 2) and
   `CACHE_TTL_SECONDS` (replaces the "cache lives until next full re-ingest" model).

2. **New `redis_client.py`** — a thin wrapper around a Redis client (e.g. `redis-py`),
   analogous to how `db.py` wraps Postgres. Add `redis` to `requirements.txt`.

3. **`rate_limit.py`** — replace the in-memory `deque`-based `check()` with a Redis
   sliding-window or fixed-window counter (e.g. `INCR` a key like
   `ratelimit:{api_key}:{current_minute}`, `EXPIRE` it at 60s, reject if the count
   exceeds the product's limit). Keep the function signature (`check(key, limit)`)
   the same so `app.py` doesn't need to change at the call site.

4. **`cache.py`** — reimplement `find_cached_answer` / `store_answer` against Redis
   instead of `qa_cache`. This is the one part of the migration that isn't a
   drop-in swap: Postgres/pgvector did an approximate-nearest-neighbor search over all
   cached questions via the `<=>` operator; Redis does not have that built in.
   Two reasonable approaches, in order of how much extra infra they need:
   - **Redis Stack / RediSearch with vector similarity** (if available on your chosen
     Redis provider) — closest like-for-like replacement, keeps the "search all past
     questions for a near match" behavior.
   - **Exact-match cache on the rewritten canonical question** (hash the canonical
     question string, use it as the Redis key directly) — much simpler, but only
     catches identical rewritten questions, not merely *similar* ones. Given
     `rewrite.py` already normalizes phrasing before this point, exact-match on the
     canonical form may catch enough of the real-world duplication to be worth trying
     first before reaching for vector support in Redis.

   Whichever is chosen, keep `kb_chunks` retrieval on Postgres/pgvector — only the
   cache moves. Don't migrate vector search wholesale to Redis; that's out of scope
   for this phase.

5. **`schema.sql`** — once the migration is verified working, drop the `qa_cache`
   table and its index from Postgres (`DROP TABLE IF EXISTS qa_cache;`). Don't drop it
   until Redis-backed caching has been running correctly for a while — keep it as a
   fallback until then.

6. **`ingest.py`** — remove the `TRUNCATE qa_cache` line (Redis TTLs handle expiry now;
   there's no longer a Postgres table to truncate).

## Verification

Re-run the full Phase 1 smoke test (from `BUILD_PLAN.md`) against the Redis-backed
version — all the same checks should still pass. Additionally:

- [ ] Scale the app service to 2+ Railway replicas and confirm the rate limit is still
      enforced correctly in aggregate (this is the specific failure mode Phase 2 fixes
      — verify it's actually fixed, not just that the single-replica case still works).
- [ ] Confirm cached answers actually expire after `CACHE_TTL_SECONDS` rather than
      persisting indefinitely.
- [ ] Confirm a knowledge-base update (`railway run python ingest.py`) no longer wipes
      the entire cache — only genuinely-affected answers should go stale (or, if using
      the exact-match approach, accept and document that stale entries persist until
      their TTL expires — that's a known trade-off of that approach, not a bug).
