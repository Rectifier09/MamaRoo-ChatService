# Build Plan — Chatbot-as-a-Service

**Read this file first.** It's the entry point; `ARCHITECTURE.md`, `API_CONTRACT.md`,
and `PHASE_2_MIGRATION.md` in this same folder go deeper on specific parts. A working
Phase 1 reference implementation is already in this folder (`app.py`, `rag_engine.py`,
`schema.sql`, etc.) — it compiles, but has **not been run against a real database or
tested end-to-end.** Your job is to stand it up, verify it, harden it, and then follow
`PHASE_2_MIGRATION.md`.

## What this service is

A shared chat API that multiple frontend products call directly from the browser,
answering end-user questions from one curated, shared knowledge base. Full context on
why each decision was made is in `ARCHITECTURE.md` — read that before changing any of
the following, since several look like they could be "simplified" but are deliberate:

- The API key sent by the browser is **public by design**, not a secret (see
  `ARCHITECTURE.md` → "Auth model"). Don't add a client-side secret or try to hide the
  key — there is no way to keep a value confidential in browser-delivered code.
- The semantic cache is checked **before** retrieval/generation, and a hit skips the
  LLM call entirely — this is the primary cost lever, not a nice-to-have.
- `ingest.py` does a **full rebuild** every run (truncates and reloads) and clears the
  semantic cache as part of that — this is intentional, not a bug to fix.

## Requirements this must satisfy

1. Knowledge base built from unstructured files (`.txt`, `.md`, `.pdf`).
2. Understand user intent, not just literal query text (query rewriting using
   conversation history, before retrieval).
3. Persist chat history per end user, per product.
4. Semantic cache: a new question similar enough to a previously-answered one reuses
   the cached answer instead of calling the LLM again.
5. Optimized for low operating cost (see `ARCHITECTURE.md` → "Cost design" for the
   four levers and why they're ordered that way).
6. Fast to build and deploy on Railway.
7. Each product authenticates with its own API key, scoped to its own allowed origins
   and rate limit.

## Phase 1 — Single Postgres (build this first)

One Railway Postgres instance (with `pgvector`) holds everything: vector chunks, chat
history, the semantic cache, and the product/API-key registry. One Railway app
service runs the FastAPI service. No Redis yet — that's Phase 2.

### File map (all present already — verify, don't recreate from scratch)

| File | Responsibility |
|---|---|
| `schema.sql` | Postgres schema: `products`, `kb_chunks`, `chat_sessions`, `chat_messages`, `qa_cache` |
| `init_db.py` | Applies `schema.sql` to `DATABASE_URL` |
| `config.py` | All environment-variable-driven settings, one place |
| `db.py` | Connection pool + `fetchone`/`fetchall`/`execute` helpers, pgvector type registered |
| `embeddings.py` | Local `sentence-transformers` embedder, shared by ingest and query time |
| `ingest.py` | Loads `.txt`/`.md`/`.pdf` from `data/knowledge_base/`, chunks, embeds, writes to `kb_chunks`; clears `qa_cache` |
| `llm_provider.py` | Provider abstraction (Anthropic + OpenAI implementations) — see `ARCHITECTURE.md` → "LLM provider abstraction" before adding a third provider or changing this interface |
| `rewrite.py` | Calls the rewrite-role provider to turn a follow-up message into a standalone question |
| `cache.py` | `find_cached_answer` / `store_answer` against `qa_cache` via pgvector similarity |
| `rag_engine.py` | Orchestrates rewrite → embed → cache check → retrieve → generate → cache write |
| `auth.py` | `get_product` FastAPI dependency: API-key lookup + origin allow-list check |
| `rate_limit.py` | In-memory per-key sliding-window limiter (single-instance only — see Phase 2) |
| `app.py` | FastAPI app: `POST /chat`, `POST /admin/products`, `GET /health` |
| `Dockerfile`, `.dockerignore`, `railway.toml` | Deploy config |
| `.env.example` | Every environment variable this service reads |

### Build checklist

Work through these in order. Each has a way to check it actually worked — don't mark
a task done without running its check.

- [ ] **Local Postgres.** Get a `pgvector`-enabled Postgres reachable locally (Railway's
      `pgvector` template works fine as a dev database too — no need to install
      Postgres locally). Put the connection string in `.env` as `DATABASE_URL`.
      *Check:* `python init_db.py` runs with no errors and prints
      `Applied N statement(s) from schema.sql`.
- [ ] **Environment.** Copy `.env.example` to `.env`, fill in `ANTHROPIC_API_KEY` and
      `ADMIN_API_KEY` (any long random string).
- [ ] **Dependencies.** `pip install -r requirements.txt` in a fresh virtualenv.
      *Check:* `python -c "import fastapi, psycopg, pgvector, sentence_transformers, anthropic"`
      raises no `ImportError`.
- [ ] **Ingest a real knowledge base.** Drop a handful of representative `.txt`/`.md`/`.pdf`
      files into `data/knowledge_base/`, run `python ingest.py`.
      *Check:* it prints a per-file chunk count and a final "Done" line; a
      `SELECT count(*) FROM kb_chunks;` in Postgres is nonzero.
- [ ] **Run the service.** `uvicorn app:app --reload --port 8000`.
      *Check:* `curl localhost:8000/health` returns `{"status":"ok"}`.
- [ ] **Provision a test product key** via `POST /admin/products` (see `API_CONTRACT.md`
      for the exact request). *Check:* response includes an `api_key` starting `pk_`.
- [ ] **End-to-end chat, cache miss.** Call `POST /chat` with a question clearly
      answerable from your ingested files. *Check:* `cached: false` in the response,
      `sources` names a real ingested file, and `chat_messages` in Postgres has two new
      rows (user + assistant) for the new `session_id`.
- [ ] **End-to-end chat, cache hit.** Immediately repeat the same question (or a
      differently-worded equivalent, e.g. "What's the refund window?" vs "How long do I
      have to get a refund?"). *Check:* `cached: true`, and the response comes back
      noticeably faster.
- [ ] **Multi-turn / intent understanding.** Ask a question, then a vague follow-up
      that only makes sense with context (e.g. "What about for annual plans?").
      *Check:* the answer addresses the follow-up correctly — this confirms
      `rewrite.py` is resolving the reference against history properly.
- [ ] **Auth enforcement.** Call `/chat` with a wrong `X-API-Key` → expect `401`. Call
      it with a valid key but a disallowed `Origin` header → expect `403`. Exceed the
      product's `rate_limit_per_minute` in a tight loop → expect `429` once the limit
      is hit.
- [ ] **Write a smoke-test script** (`tests/smoke_test.py` or similar) that automates
      the four checks above, so this doesn't have to be re-verified by hand after every
      change. This didn't exist in the reference implementation — add it.
- [ ] **Verify the provider abstraction with a real second provider**, not just by
      reading the code. Set `OPENAI_API_KEY`, `ANSWER_PROVIDER=openai`, and
      `ANSWER_MODEL` to a current OpenAI model, then re-run the cache-miss chat test.
      *Check:* it answers correctly with no code changes — only env vars moved. Set
      `ANSWER_PROVIDER` back to `anthropic` afterward (or leave both configured and
      switch freely — nothing else depends on which one is active).
- [ ] **Deploy to Railway.** Follow the "Deploying to Railway" steps in
      `ARCHITECTURE.md`. *Check:* the same smoke test passes against the live
      Railway URL.

### Definition of done for Phase 1

All checklist items above pass against the deployed Railway instance, and at least one
real product has a working API key it can call from a browser (test with a plain
`fetch()` from any static HTML page hosted anywhere, hitting the deployed `/chat`
endpoint with that key).

## Phase 2 — Add Redis (after Phase 1 is live and verified)

Move the semantic cache and rate limiter onto Redis so both work correctly across
multiple app replicas, and so cache entries can expire on a TTL instead of being
flushed all at once on every re-ingest. Full plan, including exact code changes
expected, in `PHASE_2_MIGRATION.md`. Don't start this until Phase 1's definition of
done is met — Phase 2 is additive and shouldn't require re-deriving Phase 1 decisions.

## Explicit non-goals for both phases

Don't build these unless asked — they're deferred on purpose, not overlooked:

- Hybrid/keyword search or re-ranking on top of vector retrieval.
- OCR for scanned PDFs.
- Per-product isolated knowledge bases (this is one shared KB by design).
- A real secret-based auth mode (would require a backend-per-product, which was
  explicitly ruled out — see `ARCHITECTURE.md`).
- Multi-admin roles/permissions — one shared `ADMIN_API_KEY` is the whole admin model
  for now.
- ~~Streaming responses~~ — **no longer a non-goal: shipped.** `POST /chat` accepts
  `stream: true` and responds with Server-Sent Events (see `API_CONTRACT.md`). The
  default is still one JSON response, so omitting `stream` behaves exactly as this
  plan originally described.
