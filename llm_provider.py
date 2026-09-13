"""
LLM provider abstraction. rewrite.py and rag_engine.py call through get_provider()
instead of importing anthropic (or openai, or anyone else) directly — swapping the
active provider is a config change (LLM_PROVIDER / REWRITE_PROVIDER / ANSWER_PROVIDER
in config.py), not a code change.

To add another provider (a local model server, etc.):
    1. Write a class with a `complete(system, messages, model, max_tokens) -> str` method.
    2. Add it to _PROVIDERS below.
That's the whole integration surface — nothing in rewrite.py or rag_engine.py needs
to know it exists. Gemini (`GeminiProvider`) was added this way; use it as the
template for a fourth.
"""
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
    def complete(
        self, system: list[SystemBlock], messages: list[dict], model: str, max_tokens: int
    ) -> str:
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


class OpenAIProvider:
    """
    Reference second implementation — proves the abstraction is real, not just
    theoretical. OpenAI's platform caches repeated prompt prefixes automatically (no
    explicit cache_control call needed), so the `cacheable` hint is unused here; the
    static instructions block still goes first, in case that helps the automatic
    matching, but there's no equivalent lever to pull on purpose the way there is with
    Anthropic's explicit cache_control.
    """
    def __init__(self):
        import openai

        if not config.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set but the openai provider is selected")
        self._client = openai.OpenAI(api_key=config.OPENAI_API_KEY)

    def complete(self, system, messages, model, max_tokens):
        system_text = "\n\n".join(block.text for block in system)
        full_messages = [{"role": "system", "content": system_text}, *messages]
        response = self._client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,  # `max_tokens` is deprecated on this endpoint
            messages=full_messages,
        )
        return response.choices[0].message.content or ""


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


_PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}

_instances: dict[str, LLMProvider] = {}


def get_provider(name: str) -> LLMProvider:
    if name not in _instances:
        if name not in _PROVIDERS:
            raise ValueError(f"Unknown LLM provider '{name}'. Known providers: {list(_PROVIDERS)}")
        _instances[name] = _PROVIDERS[name]()
    return _instances[name]
