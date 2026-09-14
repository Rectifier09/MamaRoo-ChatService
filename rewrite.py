"""
Rewrites the user's latest message into a standalone question before it's embedded —
both for retrieval accuracy and to normalize phrasing so a user's literal repeat, or
this same rewrite call's own deterministic-when-lucky repeat, can exact-match in the
cache (cache.py does exact-match on this rewritten text, not vector similarity — see
ARCHITECTURE.md's "Redis-backed rate limiter and cache" section). That exact-match
depends on this call producing byte-identical output for the same effective question,
which isn't guaranteed: no provider call in this codebase sets a `temperature`
parameter (llm_provider.py's `LLMProvider` Protocol doesn't have one), so a multi-turn
follow-up's cache hit rate is at the mercy of whatever determinism the provider
happens to give at its default temperature. First-turn messages (no history) skip
this call entirely via the short-circuit below, so they're unaffected.

Deliberately uses the cheap REWRITE_MODEL/REWRITE_PROVIDER: this call has a short
prompt and a short answer, so it stays negligible next to the cost of the actual
answer-generation call, regardless of which provider is in use.
"""
import config
from llm_provider import SystemBlock, get_provider

SYSTEM_PROMPT = """Rewrite the user's latest message as a standalone question that fully \
captures their intent, using the conversation so far only to resolve references \
(pronouns, "that", "it", follow-ups like "what about X"). If the message is already \
standalone, return it unchanged. Return ONLY the rewritten question — no preamble, \
no explanation, no quotes around it."""


def rewrite_query(message: str, history: list[dict] | None = None) -> str:
    if not history:
        return message.strip()

    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
    provider = get_provider(config.REWRITE_PROVIDER)
    rewritten = provider.complete(
        system=[SystemBlock(text=SYSTEM_PROMPT)],
        messages=[{"role": "user", "content": f"Conversation so far:\n{convo}\n\nLatest message: {message}"}],
        model=config.REWRITE_MODEL,
        max_tokens=200,
    )
    return rewritten.strip() or message.strip()
