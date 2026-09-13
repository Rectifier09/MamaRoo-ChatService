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

`llm_provider.py` ships two implementations — `AnthropicProvider` (default) and
`OpenAIProvider` — proving the abstraction actually works, not just that it's
theoretically pluggable. Both take the same `complete(system, messages, model,
max_tokens)` shape; `system` is a list of `SystemBlock(text, cacheable)` so each
provider can apply its own caching approach (or none) without the caller needing to
know which provider is active.

**Adding a third provider** (Gemini, a self-hosted model behind an OpenAI-compatible
gateway, etc.) means writing one class with a `complete()` method and registering it
in `llm_provider._PROVIDERS` — nothing in `rewrite.py`, `rag_engine.py`, or `app.py`
needs to change. Two things to get right when adding one:
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
intentional. `qa_cache` is truncated alongside `kb_chunks` for a correctness reason,
not a performance one: any content change can invalidate any previously cached
answer, and there's no cheap way to know which cached answers are affected by a given
content change without a lot more bookkeeping. If ingestion frequency or KB size grows
enough that a full rebuild becomes slow, revisit this — but that's a distinct problem
from the Phase 2 migration and shouldn't be bundled into it.

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

## Known limitations carried into Phase 1 on purpose

These are documented trade-offs, not bugs to silently fix — if any of them become a
real problem, that's a conversation to have explicitly, not a unilateral change:

- **Rate limiter is single-instance** (`rate_limit.py` uses an in-process dict). Correct
  only with exactly one app replica. Phase 2 fixes this.
- **No re-ranking or hybrid search** — pure vector similarity. Exact terms, IDs, and
  acronyms retrieve worse than conceptual questions.
- **No OCR** — scanned/image-only PDFs silently yield no usable text (`ingest.py`
  prints a "no extractable text" line and skips them; it doesn't error).
- **Origin allow-listing is browser-only** — native mobile apps generally don't send
  an `Origin` header, so for a mobile product, rate limiting is the only real control.
- **Single shared `ADMIN_API_KEY`** — no per-operator admin accounts or audit log on
  who created which product key.
