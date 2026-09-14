"""
Central configuration. All values can be overridden via environment variables
(or a .env file — see .env.example).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- LLM provider selection (see llm_provider.py) ---
# One default for both roles, with per-role overrides if you ever want to mix
# providers (e.g. Anthropic for rewrite, OpenAI for the final answer).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
REWRITE_PROVIDER = os.environ.get("REWRITE_PROVIDER", LLM_PROVIDER)
ANSWER_PROVIDER = os.environ.get("ANSWER_PROVIDER", LLM_PROVIDER)

# --- Provider credentials (only the one(s) you actually select need a real value) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# --- Models ---
# Model names are provider-specific — if you change *_PROVIDER, update the matching
# *_MODEL to a name that provider actually serves, or you'll get a clean error from
# that provider's SDK rather than a silent failure.
# Cheap model for the query-rewrite step (short input/output — this call is nearly free).
REWRITE_MODEL = os.environ.get("REWRITE_MODEL", "claude-haiku-4-5-20251001")
# The model that actually answers using retrieved context. Swap to a cheaper model for
# lower cost if your questions tend to be simple.
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "claude-sonnet-5")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", 1024))

# --- Embeddings ---
# Local, free, runs on CPU — no extra API key needed.
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2")
EMBEDDING_DIM = 384  # must match the model above — see schema.sql if you change it

# --- Postgres (vectors + chat history + cache + product registry, all in one DB) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Chunking ---
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", 1000))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", 150))

# --- Retrieval ---
TOP_K = int(os.environ.get("TOP_K", 4))

# --- Semantic cache ---
# Cosine *distance* (1 - similarity) below which a past answer is reused instead of
# calling the LLM again. Lower = stricter match required. 0.08 ≈ ~0.92 similarity.
CACHE_MAX_DISTANCE = float(os.environ.get("CACHE_MAX_DISTANCE", 0.08))

# --- Conversation ---
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", 6))  # user+assistant pairs kept for context

# --- Auth / admin ---
# Used to protect the /admin/products endpoint that issues new product API keys.
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

# --- Rate limiting (Redis-backed fixed-window counter — see rate_limit.py) ---
DEFAULT_RATE_LIMIT_PER_MINUTE = int(os.environ.get("DEFAULT_RATE_LIMIT_PER_MINUTE", 30))

# --- Redis (rate limiting + semantic cache — see redis_client.py) ---
REDIS_URL = os.environ.get("REDIS_URL", "")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", 86400))  # 24h
