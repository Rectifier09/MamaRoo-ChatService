"""
LLM provider abstraction. rewrite.py and rag_engine.py call through get_provider()
instead of importing anthropic (or openai, or anyone else) directly — swapping the
active provider is a config change (LLM_PROVIDER / REWRITE_PROVIDER / ANSWER_PROVIDER
in config.py), not a code change.

To add another provider:
    1. If it speaks OpenAI's chat.completions API shape (most "OpenAI-compatible"
       gateways do — Groq, Together, Fireworks, a self-hosted vLLM server, etc.),
       subclass _OpenAICompatibleProvider and set two class attributes: `base_url`
       and `api_key_config_name`. See GroqProvider below — that's the whole class.
    2. Otherwise, write a class with its own `complete(system, messages, model,
       max_tokens) -> str` method from scratch (see AnthropicProvider, GeminiProvider).
    3. Either way, add the class to _PROVIDERS below and its API key to config.py.
`complete()` is the whole *required* integration surface — nothing in rewrite.py or
rag_engine.py needs to know the class exists.

Optionally, a provider MAY also implement `stream_complete(system, messages, model,
max_tokens) -> Iterator[str]`, yielding answer text incrementally, to serve
`POST /chat` requests with `stream: true` (see StreamingLLMProvider below). This is
a duck-typed capability, discovered at runtime via `hasattr` — see
rag_engine.answer_provider_supports_streaming(). Omitting it is a supported choice
(AnthropicProvider deliberately omits it): that provider simply can't serve
`stream: true`, which app.py turns into a clean JSON 500 ("Provider 'x' does not
support streaming"), not a crash, and leaves non-streaming requests unaffected.
"""
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import config


@dataclass
class SystemBlock:
    """One piece of the system prompt. `cacheable=True` is a hint for providers that
    support explicit prompt-caching markup (Anthropic); providers without that concept
    just ignore the flag and use the text as-is."""
    text: str
    cacheable: bool = False


class LLMProvider(Protocol):
    """The required provider surface: one `complete()` method. Every entry in
    _PROVIDERS satisfies this."""

    def complete(
        self, system: list[SystemBlock], messages: list[dict], model: str, max_tokens: int
    ) -> str:
        ...


class StreamingLLMProvider(LLMProvider, Protocol):
    """The *optional* extra surface a provider may implement on top of LLMProvider:
    incremental generation for `POST /chat` with `stream: true`.

    Deliberately a separate Protocol rather than a method on LLMProvider — not every
    provider implements it (AnthropicProvider doesn't), and folding it into
    LLMProvider would make those providers fail a type check for a capability they
    are allowed to skip. Nothing annotates against this Protocol today; the
    capability is checked at runtime by duck typing
    (`hasattr(provider, "stream_complete")` — see
    rag_engine.answer_provider_supports_streaming). This class exists so the
    optional capability, and its exact signature, are declared in one obvious place
    for anyone adding a provider.
    """

    def stream_complete(
        self, system: list[SystemBlock], messages: list[dict], model: str, max_tokens: int
    ) -> Iterator[str]:
        ...


class AnthropicProvider:
    def __init__(self):
        import anthropic

        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set but the anthropic provider is selected")
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def complete(self, system, messages, model, max_tokens):
        system_param = [
            {
                "type": "text",
                "text": block.text,
                **({"cache_control": {"type": "ephemeral"}} if block.cacheable else {}),
            }
            for block in system
        ]
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_param,
            messages=messages,
        )
        return "".join(block.text for block in response.content if block.type == "text")


class _OpenAICompatibleProvider:
    """
    Shared base for any provider that speaks OpenAI's chat.completions API shape —
    OpenAI itself, and any "OpenAI-compatible" gateway (Groq, Together, Fireworks,
    a self-hosted vLLM server, ...) that implements the same endpoint shape against
    a different `base_url`. A new one of these is a 4-line subclass (see
    GroqProvider) — no new HTTP/request logic, since the `openai` SDK already
    supports a custom `base_url`.

    None of these platforms have an explicit prompt-caching call to make here (no
    equivalent to Anthropic's `cache_control`), so the `cacheable` hint is unused —
    the static instructions block still goes first in case that helps automatic
    prefix-based caching where the platform does it, but there's no lever to pull
    on purpose.

    Subclasses set:
        base_url            — None for OpenAI's own default, or the gateway's URL
        api_key_config_name — the config.py attribute name holding the API key
    """
    base_url: str | None = None
    api_key_config_name: str = "OPENAI_API_KEY"

    def __init__(self):
        import openai

        api_key = getattr(config, self.api_key_config_name, "")
        if not api_key:
            raise RuntimeError(f"{self.api_key_config_name} is not set but this provider is selected")
        self._client = openai.OpenAI(api_key=api_key, base_url=self.base_url)

    def complete(self, system, messages, model, max_tokens):
        system_text = "\n\n".join(block.text for block in system)
        full_messages = [{"role": "system", "content": system_text}, *messages]
        response = self._client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,  # `max_tokens` is deprecated on this endpoint
            messages=full_messages,
        )
        return response.choices[0].message.content or ""

    def stream_complete(self, system, messages, model, max_tokens):
        system_text = "\n\n".join(block.text for block in system)
        full_messages = [{"role": "system", "content": system_text}, *messages]
        stream = self._client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
            messages=full_messages,
            stream=True,
        )
        for chunk in stream:
            # Some OpenAI-compatible gateways end the stream with a bookkeeping
            # chunk that carries no choices at all (usage stats only). Indexing
            # [0] on that raises IndexError and turns a completion that actually
            # succeeded into a terminal error event -- skip it instead.
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


class OpenAIProvider(_OpenAICompatibleProvider):
    """Reference second implementation — proves the abstraction is real, not just
    theoretical. Uses OpenAI's own default base_url."""
    api_key_config_name = "OPENAI_API_KEY"


class GroqProvider(_OpenAICompatibleProvider):
    """
    Groq-hosted open-weight models (e.g. openai/gpt-oss-120b) over its
    OpenAI-compatible endpoint. Added for two reasons: Groq's inference is fast,
    and — the reason it matters here — it's a completely independent quota pool
    from Gemini, so splitting REWRITE_PROVIDER=groq / ANSWER_PROVIDER=gemini (or
    either alone) means one provider's free-tier daily cap doesn't take down the
    whole service. See ARCHITECTURE.md's "LLM provider abstraction" section.
    """
    base_url = "https://api.groq.com/openai/v1"
    api_key_config_name = "GROQ_API_KEY"


class GeminiProvider:
    """
    Third implementation, added following this file's own extension pattern (see
    module docstring): one class with a complete() method, registered below.
    Gemini has no explicit prompt-caching call to make here (unlike Anthropic's
    cache_control), so `cacheable` is unused — same stance as OpenAIProvider.
    """
    def __init__(self):
        from google import genai

        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set but the gemini provider is selected")
        self._client = genai.Client(api_key=config.GEMINI_API_KEY)

    def complete(self, system, messages, model, max_tokens):
        from google.genai import types

        system_text = "\n\n".join(block.text for block in system)
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part.from_text(text=m["content"])],
            )
            for m in messages
        ]
        response = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_text,
                max_output_tokens=max_tokens,
                # Gemini 2.5+ "thinking" models spend max_output_tokens on internal
                # reasoning before emitting visible text — a short rewrite/answer
                # budget can be entirely consumed by thinking, returning empty text
                # (finish_reason=MAX_TOKENS, thoughts_token_count>0, .text is None).
                # This service doesn't need chain-of-thought, and burning tokens on
                # it fights the cost-minimization design everywhere else in
                # rag_engine.py — disabled outright. (Pro-tier models don't allow
                # thinking_budget=0; drop this if ANSWER_MODEL is ever set to one.)
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return response.text or ""

    def stream_complete(self, system, messages, model, max_tokens):
        from google.genai import types

        system_text = "\n\n".join(block.text for block in system)
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part.from_text(text=m["content"])],
            )
            for m in messages
        ]
        stream = self._client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_text,
                max_output_tokens=max_tokens,
                # Same thinking-budget issue as complete() above applies to
                # streaming too -- without this, a chunk can arrive with
                # thinking content and no visible text.
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        for chunk in stream:
            if chunk.text:
                yield chunk.text


_PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
}

_instances: dict[str, LLMProvider] = {}


def get_provider(name: str) -> LLMProvider:
    if name not in _instances:
        if name not in _PROVIDERS:
            raise ValueError(f"Unknown LLM provider '{name}'. Known providers: {list(_PROVIDERS)}")
        _instances[name] = _PROVIDERS[name]()
    return _instances[name]
