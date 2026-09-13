-- Run once via `python init_db.py` (or paste into Railway's Postgres query editor).

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per product/frontend that's allowed to call this service.
CREATE TABLE IF NOT EXISTS products (
    id                     SERIAL PRIMARY KEY,
    name                   TEXT NOT NULL,
    api_key                TEXT UNIQUE NOT NULL,
    -- Origins allowed to call the API with this key from a browser, e.g.
    -- '{https://mamaroo.app,http://localhost:5173}'. Use '{*}' to allow any origin
    -- (fine for local testing, not for production).
    allowed_origins        TEXT[] NOT NULL DEFAULT '{}',
    rate_limit_per_minute  INTEGER NOT NULL DEFAULT 30,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The shared knowledge base. `ingest.py` truncates and rebuilds this each run.
CREATE TABLE IF NOT EXISTS kb_chunks (
    id           SERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    content      TEXT NOT NULL,
    embedding    vector(384) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
    ON kb_chunks USING hnsw (embedding vector_cosine_ops);

-- One row per conversation. Ties an end user (as identified by the calling product)
-- to a product and a growing list of messages.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id           SERIAL PRIMARY KEY,
    product_id   INTEGER NOT NULL REFERENCES products(id),
    end_user_id  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_sessions_product_user_idx
    ON chat_sessions (product_id, end_user_id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          SERIAL PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES chat_sessions(id),
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session_idx ON chat_messages (session_id, id);

-- Semantic cache, shared across all products (same KB → same answer regardless of
-- which product asked). `ingest.py` clears this on every rebuild since a content
-- change can invalidate any cached answer.
CREATE TABLE IF NOT EXISTS qa_cache (
    id                  SERIAL PRIMARY KEY,
    canonical_question  TEXT NOT NULL,
    question_embedding  vector(384) NOT NULL,
    answer              TEXT NOT NULL,
    sources             TEXT[] NOT NULL DEFAULT '{}',
    hit_count           INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qa_cache_embedding_idx
    ON qa_cache USING hnsw (question_embedding vector_cosine_ops);
