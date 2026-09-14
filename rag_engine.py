"""
Core retrieval-augmented generation logic:
    rewrite -> check semantic cache -> (on miss) embed -> retrieve + generate -> cache the result
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
    # ::vector cast required: psycopg has no destination-column type to infer
    # from in a bare comparison expression, so a plain Python list parameter
    # defaults to `double precision[]`, and Postgres rejects
    # `vector <=> double precision[]` outright without an explicit cast.
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


def answer_provider_supports_streaming() -> bool:
    return hasattr(get_provider(config.ANSWER_PROVIDER), "stream_complete")


def generate_answer_stream(message: str, history: list[dict] | None = None):
    """Yields ("delta", str) tuples as answer text becomes available, then
    exactly one ("done", dict): {"sources": [...], "cached": bool} on
    success, or {"error": str} if generation failed after streaming began.
    Exactly one "done" is always yielded, including when the failure happens
    after deltas were already sent (a provider error mid-stream, an empty
    generation, or the cache write failing) -- the stream never just stops.
    Exceptions from rewrite/embed/cache-lookup/retrieve are NOT caught here
    -- they propagate out of the generator's first next() call so the caller
    can distinguish "failed before anything was sent" from "failed mid-
    stream" (see app.py)."""
    canonical = rewrite_query(message, history)

    cached = cache.find_cached_answer(canonical)
    if cached:
        yield "delta", cached["answer"]
        yield "done", {"sources": list(cached["sources"]), "cached": True}
        return

    query_embedding = embed_texts([canonical])[0]
    chunks = retrieve(query_embedding)
    system = [
        SystemBlock(text=SYSTEM_INSTRUCTIONS, cacheable=True),
        SystemBlock(text=_build_context(chunks), cacheable=False),
    ]

    messages = list(history or [])
    messages.append({"role": "user", "content": message})

    provider = get_provider(config.ANSWER_PROVIDER)
    sources = sorted({meta.get("source") for _, meta in chunks})
    full_answer = ""
    # store_answer lives INSIDE this try on purpose: by the time it runs, real
    # delta events have already reached the client, so a failure there (DB blip,
    # pool timeout -- db.py's pool caps at max_size=5) must still produce a
    # terminal event rather than an exception escaping the generator and leaving
    # the stream dangling with nothing.
    try:
        for delta in provider.stream_complete(
            system=system,
            messages=messages,
            model=config.ANSWER_MODEL,
            max_tokens=config.MAX_TOKENS,
        ):
            full_answer += delta
            yield "delta", delta

        if full_answer:
            cache.store_answer(canonical, full_answer, sources)
    except Exception as exc:
        yield "done", {"error": str(exc)}
        return

    if not full_answer:
        # The provider returned cleanly but emitted nothing -- reachable with
        # reasoning models (Groq's openai/gpt-oss-120b) whose reasoning can
        # consume the whole MAX_TOKENS budget before any visible text, the same
        # failure class as Gemini's thinking_budget issue handled in
        # llm_provider.py. Caching "" (above) would poison the Redis-backed
        # cache, which is shared with the non-streaming path (see
        # ARCHITECTURE.md): later
        # semantically-similar questions -- including plain non-streaming ones
        # -- would come back 200 with an empty answer. So: write nothing, and
        # report it as the failure it is.
        yield "done", {"error": "empty response from provider"}
        return

    yield "done", {"sources": sources, "cached": False}
