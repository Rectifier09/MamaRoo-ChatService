"""
Core retrieval-augmented generation logic:
    rewrite -> embed -> check semantic cache -> (on miss) retrieve + generate -> cache the result
"""
import cache
import config
import db
from embeddings import embed_texts
from llm_provider import SystemBlock, get_provider
from rewrite import rewrite_query

# Kept as a separate, always-identical block so a provider with explicit prompt-caching
# support (Anthropic) can cache it across requests — only the retrieved context below
# it changes per question. Providers without that concept (see llm_provider.py) just
# treat this the same as any other system text.
SYSTEM_INSTRUCTIONS = """You are a helpful assistant that answers questions using ONLY the \
knowledge base excerpts provided in the next message block.

Rules:
- If the excerpts don't contain the answer, say you don't have that information — don't \
guess or use outside knowledge.
- Keep answers concise and directly responsive to the question.
- When useful, mention which source the information came from."""


def retrieve(query_embedding, k: int = None) -> list[tuple[str, dict]]:
    k = k or config.TOP_K
    # ::vector cast required — see the matching comment in cache.py's
    # find_cached_answer for why a bare `<=>` comparison needs it.
    rows = db.fetchall(
        "SELECT source, content FROM kb_chunks ORDER BY embedding <=> %s::vector LIMIT %s",
        (query_embedding, k),
    )
    return [(r["content"], {"source": r["source"]}) for r in rows]


def _build_context(chunks: list[tuple[str, dict]]) -> str:
    if not chunks:
        return "(no knowledge base content indexed yet)"
    blocks = [f"[Source: {meta.get('source')}]\n{doc}" for doc, meta in chunks]
    return "Knowledge base excerpts:\n\n" + "\n\n---\n\n".join(blocks)


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
