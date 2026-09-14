# Architecture — Chatbot-as-a-Service

This explains *why* the system is shaped the way it is. Referenced from `BUILD_PLAN.md`.
If a change seems like an obvious improvement but contradicts something here, it's
probably not obvious — re-check before making it.

## Request flow

```
Browser (product A, B, or C's frontend)
   │  POST /chat   header: X-API-Key
   ▼
auth.get_product        → looks up the key in `products`; if the product has a
                           non-wildcard allowed_origins list, checks the request's
                           Origin/Referer header against it
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
(app.py)                 → logs both turns to chat_messages
                          → returns {answer, sources, session_id, cached}
```

## Why one shared knowledge base

Confirmed with the product owner: all products draw on the same curated content, so
`kb_chunks` has no per-product scoping. This has a real benefit beyond simplicity —
the semantic cache is also shared, so traffic from *any* product warms the cache for
*all* of them. Don't add a `product_id` column to `kb_chunks` or `qa_cache` without
re-confirming this is still the intent; if it changes, both tables need it, and
retrieval/cache-lookup queries need a `WHERE product_id = ...` clause.

## Auth model — the API key is public, not a secret

**This was a deliberate call, made after confirming that end-user browsers call this
API directly** (no per-product backend sits in front of it). That single fact rules
out real secrets: anything shipped to a browser — in a JS bundle, a network request,
anywhere — can be read by the person using that browser. There is no code-level fix
for this; it's a property of where the code runs, not how carefully the key is
handled.

Given that, the design is deliberately the same shape as a Stripe *publishable* key or
a Google Maps *browser* key:

- `products.api_key` identifies which product is calling and which knowledge-base
  config / rate limit applies. It is not expected to be confidential.
- `products.allowed_origins` restricts which domains a key works from. This stops
  casual reuse (someone copying a key into their own unrelated site) but does **not**
  stop a non-browser client from replaying the key with a forged `Origin` header —
  that's a known, accepted gap for a purely client-side integration.
- `rate_limit_per_minute` caps the worst-case cost exposure from a leaked or scraped
  key, which is the more important control given the above.

**If a future product needs real confidentiality** (e.g., gating access behind a paid
plan), the correct fix is a thin backend for that product that calls this service
server-to-server with a real secret — not adding a secret to this service's public
API. Don't build that speculatively; wait until a product actually needs it.

## Cost design, in priority order

Implemented in `rag_engine.py`, in this order because that's their order of impact:

1. **Semantic cache (`cache.py`)** — a hit avoids the generation LLM call entirely.
   This is a 100% savings on that request, not a discount, which is why it's checked
   before anything else in `rag_engine.generate_answer`.
2. **Model tiering** — `rewrite.py` uses `config.REWRITE_MODEL` (Haiku by default):
   short prompt, short output, run on every request but cheap enough not to matter.
   `config.ANSWER_MODEL` (Sonnet by default) is only invoked on a cache miss.
3. **Prompt caching, where the provider supports it** — in `rag_engine.py`,
   `SYSTEM_INSTRUCTIONS` is passed as its own `SystemBlock(cacheable=True)`, separate
   from the retrieved-context block that changes every request. `AnthropicProvider`
   turns that into an explicit `cache_control` marker (~90% off the input-token cost
   of that block on a hit); `OpenAIProvider` ignores the flag since OpenAI's caching
   is automatic based on prefix matching, not an explicit call. Either way, this only
   helps the fixed instructions block, not the retrieved context — it's a small
   stacked win on top of #1 and #2, not a substitute for them.
4. **Local embeddings (`embeddings.py`)** — `sentence-transformers` running locally
   means no per-call cost and no second API key for the retrieval/cache-check path.
   The trade-off is a larger Docker image (`sentence-transformers` pulls in PyTorch,
   ~1-2GB) — accepted deliberately in exchange for not needing a Voyage AI (or similar)
   account and key just to get started.

## LLM provider abstraction

Neither `rewrite.py` nor `rag_engine.py` imports `anthropic` (or any other vendor SDK)
directly — both call `llm_provider.get_provider(name).complete(...)`. `config.py`
controls which provider serves each role independently:

```
LLM_PROVIDER      # default for both roles if the two below aren't set
REWRITE_PROVIDER  # defaults to LLM_PROVIDER
ANSWER_PROVIDER   # defaults to LLM_PROVIDER
```

`llm_provider.py` ships four implementations — `AnthropicProvider`, `OpenAIProvider`,
`GeminiProvider`, and `GroqProvider` — proving the abstraction actually works, not
just that it's theoretically pluggable. All take the same `complete(system, messages,
model, max_tokens)` shape; `system` is a list of `SystemBlock(text, cacheable)` so each
provider can apply its own caching approach (or none) without the caller needing to
know which provider is active.

Because each provider is an independent quota pool, `REWRITE_PROVIDER` and
`ANSWER_PROVIDER` can be hotswitched independently when one vendor's free tier runs
out — which is exactly what happened on 2026-09-13, when Gemini's free-tier daily cap
was exhausted and both roles were pointed at Groq (see the comment in `.env`). One
caveat to record while that hotswitch is in place: `GeminiProvider.stream_complete()`
has been verified by code review only — it is structurally identical to the already
live-proven `complete()` method, including the `thinking_budget=0` fix without which a
Gemini 2.5+ model can spend the whole `max_output_tokens` budget on reasoning and emit
no visible text — but it has **not** been exercised against the live Gemini API,
because the daily quota was exhausted during this work. Streaming via the Gemini
hotswitch is therefore code-reviewed but not live-verified as of this writing;
streaming on Groq is live-verified end to end by `tests/smoke_test.py`.

**Adding another provider** (a self-hosted model behind an OpenAI-compatible
gateway, etc.) means writing one class with a `complete()` method and registering it
in `llm_provider._PROVIDERS` — nothing in `rewrite.py`, `rag_engine.py`, or `app.py`
needs to change. `complete()` is the only *required* method; a provider may
additionally implement `stream_complete(system, messages, model, max_tokens) ->
Iterator[str]` to serve `POST /chat` with `stream: true`. That's an optional,
duck-typed capability (`hasattr`-checked by
`rag_engine.answer_provider_supports_streaming()`, declared for readers as the
`StreamingLLMProvider` Protocol in `llm_provider.py`), deliberately not part of the
required surface: `AnthropicProvider` omits it, and a provider that omits it simply
returns a clean JSON `500` for `stream: true` requests while non-streaming requests
keep working normally. Two things to get right when adding one:
- `messages` here only ever contains plain `{"role": "user"/"assistant", "content": str}`
  entries (no tool calls, no images) — if a new provider's SDK wants a different
  message shape, translate it inside that provider's `complete()`, not upstream.
- Model names are provider-specific. Swapping `*_PROVIDER` without also updating the
  matching `*_MODEL` to a name that provider actually serves will fail loudly at the
  API call (wrong model string) rather than silently — that's intentional; don't add
  cross-provider model-name translation, it's not worth the complexity for two knobs
  a human sets together anyway.

## Why `ingest.py` truncates everything on every run

Chunks don't carry a stable, deterministic ID tied to file path + chunk index in this
version — a full rebuild sidesteps needing one, at the cost of `ingest.py` being
non-incremental (every run re-embeds the entire knowledge base, even unchanged files).
Given the knowledge base here is small and updates are infrequent, that trade-off is
intentional. If ingestion frequency or KB size grows enough that a full rebuild
becomes slow, revisit this.

**`ingest.py` no longer touches the cache.** Before the Phase 2 Redis migration (see
"Redis-backed rate limiter and cache" below), `qa_cache` was truncated alongside
`kb_chunks` on every run for a correctness reason, not a performance one: any content
change could invalidate any previously cached answer, and there was no cheap way to
know which cached answers were affected by a given content change without a lot more
bookkeeping. That guarantee no longer holds: **a KB re-ingest does NOT invalidate
previously-cached answers.** A cached answer can now go on serving for up to
`CACHE_TTL_SECONDS` (24h default) after the knowledge base content it was based on
has changed, with no automatic flush — old entries simply age out on their own
schedule, independent of when the KB was last updated.

If a re-ingest changes something where serving a stale cached answer for up to
`CACHE_TTL_SECONDS` would matter, flush the cache manually afterward:

```bash
redis-cli -u $REDIS_URL --scan --pattern 'qa_cache:*' | xargs -r redis-cli -u $REDIS_URL del
```

## Deploying to Railway (Phase 1)

Two services in one Railway project:

1. **Postgres** — deploy Railway's `pgvector` template from the template gallery (the
   default Postgres template does not have `pgvector` installed).
2. **This app** — connect the GitHub repo, or `railway up` from this folder. Railway
   auto-detects `Dockerfile`.

In the app service's Variables tab, set:
- `ANTHROPIC_API_KEY`
- `ADMIN_API_KEY`
- `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (Railway's reference-variable syntax,
  assuming the Postgres service is named `Postgres` in the project)

Then, via `railway run` (executes inside the deployed environment, with access to the
same env vars):

```bash
railway run python init_db.py
railway run python ingest.py
```

Generate a public domain under Settings → Networking → Generate Domain. Re-running
`railway run python ingest.py` after editing `data/knowledge_base/` and pushing an
updated deploy is how the knowledge base gets refreshed — no rebuild/redeploy of the
image is required for that step, since the KB lives in Postgres, not baked into the
image.

## Redis-backed rate limiter and cache (Phase 2)

`rate_limit.py` and `cache.py` moved off in-process state / Postgres onto
Redis (`redis_client.py`). Two independent fixes:

- **Rate limiter**: was an in-process Python dict, correct only with exactly
  one app replica (each replica counted independently, silently multiplying
  the effective limit by replica count). Now a Redis `INCR`/`EXPIRE`
  fixed-window counter, shared and atomic across every replica.
  `rate_limit.check(key, limit)`'s signature and normal-path behavior from
  the caller's perspective (raises `429` past the limit) are unchanged.
  One new behavior: a Redis connection/timeout error makes it fail OPEN
  (logs a warning, allows the request through) rather than raising an
  uncaught error into `/chat` — see "Redis as a dependency" below.
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

**Redis as a dependency**: `/chat` now depends on Redis for both the rate
limiter and the cache. `rate_limit.check()` and `cache.find_cached_answer()`/
`cache.store_answer()` all fail OPEN on a Redis error (`redis.RedisError`) —
a warning is logged, and the request proceeds as if the rate limiter allowed
it / the cache missed, rather than the endpoint 500ing. This trades a small
amount of correctness (uncapped rate limiting, no caching) for availability
during a Redis outage, consistent with this limiter's existing posture of
capping worst-case cost exposure rather than being a precise, security-
critical throttle (see the "Rate limiter" trade-off above). `/health` stays
dependency-free by design and does not check Redis, so a Redis outage does
not turn it unhealthy — that's a deliberate choice given the endpoints above
now degrade gracefully instead of failing.

**Limitation: multi-turn cache hit rate depends on rewrite determinism.**
The cache key is a hash of the canonical (rewritten) question, and for any
multi-turn follow-up that canonical text comes from an LLM call
(`rewrite.py`). No provider call in this codebase sets a `temperature`
parameter — `llm_provider.py`'s `LLMProvider` Protocol and its three
implementations (`AnthropicProvider`, `OpenAIProvider`/`GroqProvider`,
`GeminiProvider`) have no such parameter — so the rewrite call isn't
guaranteed to produce byte-identical output for the same effective
question across turns, and an exact-match cache is sensitive to exactly
that. First-turn (no-history) messages skip the rewrite call entirely via
`rewrite.py`'s `if not history: return message.strip()` short-circuit, so
they're unaffected — this only softens the hit rate for follow-up turns.
Adding deterministic rewriting (threading a `temperature` parameter through
the `LLMProvider` Protocol and all three implementations) is a real, scoped
follow-up; it wasn't attempted as part of this migration.

## Known limitations carried into Phase 1 on purpose

These are documented trade-offs, not bugs to silently fix — if any of them become a
real problem, that's a conversation to have explicitly, not a unilateral change:

- **No re-ranking or hybrid search** — pure vector similarity. Exact terms, IDs, and
  acronyms retrieve worse than conceptual questions.
- **No OCR** — scanned/image-only PDFs silently yield no usable text (`ingest.py`
  prints a "no extractable text" line and skips them; it doesn't error).
- **Origin allow-listing is browser-only** — native mobile apps generally don't send
  an `Origin` header, so for a mobile product, rate limiting is the only real control.
- **Single shared `ADMIN_API_KEY`** — no per-operator admin accounts or audit log on
  who created which product key.
