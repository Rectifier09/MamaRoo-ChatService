# Streaming Responses — Design

**Status:** Approved (design confirmed in chat 2026-09-13, simplified per user
request before final approval). Not yet implemented.
**Author:** Claude (session with the project owner), 2026-09-13.

## Problem

`POST /chat` returns the full answer in one response, after the entire LLM
generation completes. For longer answers this means the user stares at a
loading state for the whole generation time with no feedback. This spec adds
token-by-token streaming as an **opt-in** addition to the existing endpoint —
the current JSON contract is completely unchanged for any client that doesn't
ask for streaming, which matters because an external developer (the user's
friend) is already building a frontend against today's contract per
`FRONTEND_INTEGRATION.md`.

## Non-goals (explicitly out of scope for this spec)

- Streaming the `rewrite.py` step — it's a fast internal step, not something
  a user watches; it stays exactly as it is today (a single blocking call).
- `stream_complete()` on `AnthropicProvider`/`OpenAIProvider` — neither has a
  configured key, so streaming code for them couldn't be verified live. They
  keep working via `complete()` only, same as `rewrite.py`'s non-streaming
  role usage today. Add their `stream_complete()` if/when one is actually
  configured.
- Artificially pacing a cache-hit response to *look* like a real generation —
  a cache hit sends its one chunk immediately (see Cache behavior below).
- Any change to `GET /chat/sessions` or `GET /chat/sessions/{id}/messages`
  (shipped separately, unaffected by this spec).
- Rich media (still unscoped from an earlier discussion — unrelated to this
  spec).

## API design

### `POST /chat` — new optional field

```json
{
  "message": "What is Braxton Hicks?",
  "end_user_id": "...",
  "session_id": null,
  "stream": false
}
```
- `stream` (boolean, optional, default `false`) — when absent or `false`,
  behavior and response are **byte-for-byte identical to today**: a single
  `200 application/json` body per `API_CONTRACT.md`'s existing `/chat`
  documentation. This spec changes nothing about that path.
- When `true`, the response is `200 text/event-stream` instead, per the
  event schema below.

### Streaming response — SSE, 2 event types

Content-Type: `text/event-stream`. Each event is a `data:` line with a JSON
payload (no named `event:` framing — a single event type keeps client
parsing to one `onmessage`-style handler with a `type` discriminator field is
avoided too; simplicity here is: two possible payload shapes, distinguished
by which fields are present, described below).

1. **Zero or more delta events** — sent as answer text becomes available:
   ```
   data: {"delta": "Braxton Hicks are"}

   data: {"delta": " irregular, mild..."}

   ```
   For a cache hit, exactly **one** delta event is sent, containing the
   entire cached answer text (see Cache behavior).

2. **Exactly one terminal event** — either success or mid-stream failure:
   - Success:
     ```
     data: {"done": true, "session_id": 42, "sources": ["Mayo-Clinic-Pregnancy-Library.md"], "cached": false}
     ```
   - Mid-stream failure (only possible once streaming has already started —
     see Error handling):
     ```
     data: {"done": true, "error": "<message>"}
     ```
     When `error` is present, `session_id`/`sources`/`cached` are omitted —
     the client should treat the partial `delta` text received so far as
     incomplete/unreliable.

A client reads the stream, accumulates `delta` text for display, and stops
on the event carrying `done: true` (checking for `error` to distinguish
success from mid-stream failure).

### Error handling — two distinct cases

- **Failure before any streaming has started** (auth, `400` empty message,
  rewrite/retrieval/cache-lookup failure, or the LLM call fails on its very
  first chunk before anything was ever written to the response): return a
  normal JSON error response with the appropriate status code (`400`, `401`,
  `403`, `429`, `500`), **exactly like the non-streaming path today**. The
  HTTP status line hasn't been committed to `text/event-stream` yet, so this
  is just today's existing error handling, unconditionally — `stream: true`
  changes nothing about pre-stream error behavior.
- **Failure after streaming has already started** (at least one `delta` event
  was already sent, then the LLM call errors mid-generation): the HTTP status
  is already `200 text/event-stream` and can't change. Send the terminal
  `{"done": true, "error": "..."}` event and end the stream. Do **not** write
  anything to `chat_messages` or `qa_cache` for this turn — a partial,
  truncated answer must never be persisted as if it were the real answer or
  cached for future reuse.

## Cache behavior

On a cache hit, the full cached answer is already sitting in Postgres before
any LLM call would happen — there's nothing to stream token-by-token. Send it
as a **single delta event** containing the whole text, immediately followed
by the terminal `done` event (`cached: true`). No artificial delay, no fake
per-token pacing — a cache hit is the fast path and should visibly be fast.

## Provider layer

Add `stream_complete(system, messages, model, max_tokens) -> Iterator[str]`
to `GeminiProvider` and `GroqProvider` (via their shared base for Groq/OpenAI
— see note below) only. Each yields plain text chunks as they arrive from
that SDK's own streaming API:

- **`GroqProvider`** (and, if ever configured, `OpenAIProvider` — both go
  through `_OpenAICompatibleProvider`): `client.chat.completions.create(...,
  stream=True)` returns an iterator of chunks; yield
  `chunk.choices[0].delta.content` when non-empty/non-None.
- **`GeminiProvider`**: `client.models.generate_content_stream(...)` (same
  `GenerateContentConfig`, including the existing `thinking_config`
  workaround already in place for the non-streaming path — the thinking-
  budget issue applies identically to streaming) returns an iterator of
  response chunks; yield `chunk.text` when non-empty/non-None.

`stream_complete` is added to `_OpenAICompatibleProvider` (the shared base),
so both `OpenAIProvider` and `GroqProvider` get it automatically — same
reasoning as why `complete()` lives there today. This keeps the "any
OpenAI-compatible gateway is a 4-line subclass" property intact for
streaming too, even though only `GroqProvider` is actually configured right
now.

`AnthropicProvider` gets no `stream_complete` method at all (not even a
`NotImplementedError` stub — YAGNI; add it when there's a real key to verify
it against).

## Orchestration

New function in `rag_engine.py`, alongside the existing `generate_answer`:

```python
def generate_answer_stream(message: str, history: list[dict] | None = None):
    """Yields (kind, payload) tuples: ("delta", str) then exactly one
    ("done", dict) with keys {sources, cached} or {error}."""
```

Same rewrite → embed → cache-check → retrieve flow as `generate_answer`:
- Cache hit: yield `("delta", cached_answer)`, then
  `("done", {"sources": cached["sources"], "cached": True})`.
- Cache miss: retrieve chunks, build the same system/messages as today, call
  `get_provider(config.ANSWER_PROVIDER).stream_complete(...)`, yielding
  `("delta", chunk)` for each piece; accumulate the full text as it goes.
  On successful completion: call `cache.store_answer(...)` with the full
  accumulated text (same as `generate_answer` does today), then yield
  `("done", {"sources": sources, "cached": False})`.
  On an exception raised mid-iteration: yield `("done", {"error": str(exc)})`
  and **do not** call `cache.store_answer`.
- If `config.ANSWER_PROVIDER` doesn't support `stream_complete` (i.e. it's
  `"anthropic"` and someone sets `stream: true` anyway): raise before
  yielding anything. Python generators are lazy — an exception raised inside
  one only fires on its first `next()`. `app.py` must therefore check
  provider support *before* constructing the `StreamingResponse` at all
  (e.g. `hasattr(get_provider(config.ANSWER_PROVIDER), "stream_complete")`),
  not rely on catching an exception from a generator already handed to
  Starlette — by the time Starlette starts iterating a `StreamingResponse`'s
  generator, the `200`/`text/event-stream` status is already committed to
  the client, so a normal `500 JSON` error is only possible if this check
  happens synchronously beforehand.

## `app.py` changes

`POST /chat`'s handler branches on `req.stream` immediately after the
existing auth/rate-limit/validation/session-lookup logic (all unchanged):

- `stream=False` (or absent): exactly today's code path, unchanged.
- `stream=True`: return a `StreamingResponse` (`media_type="text/event-stream"`)
  wrapping a generator that:
  1. Consumes `rag_engine.generate_answer_stream(...)`, formatting each
     `("delta", text)` as `f"data: {json.dumps({'delta': text})}\n\n"` and
     yielding it immediately (so the client sees it as it's produced, not
     buffered).
  2. Accumulates the full delta text as it streams.
  3. On the `("done", {...})` tuple: if no `error` key, write both
     `chat_messages` rows (user + full accumulated assistant text) — same
     `db.execute` calls the non-streaming path already makes — **then**
     yield the terminal `data: {...}\n\n` line. If there's an `error` key,
     skip the `chat_messages` write entirely and yield the error terminal
     line.

This mirrors the non-streaming handler's own DB-write ordering (write to
`chat_messages` only after the answer is fully known) — streaming doesn't
change *what* gets persisted or *when* relative to "the answer is complete،"
only how the client observes the text arriving.

## Testing plan

1. **Local, against the real dev Postgres and real Gemini/Groq calls** (same
   no-mocking pattern as every other test this session):
   - `stream: false` (and omitted entirely) → confirm response is
     byte-identical in shape to before this change (regression check that
     the opt-in didn't disturb the existing path).
   - `stream: true`, cache miss → confirm multiple `delta` events arrive
     over time (not all at once — proves real streaming, not a buffered
     fake), the accumulated text matches what a non-streaming call to the
     same question would produce in content/quality, and the terminal event
     has the correct `session_id`/`sources`/`cached: false`.
   - `stream: true`, cache hit (ask the same question again) → confirm
     exactly one `delta` event with the full cached text, then `done` with
     `cached: true`.
   - `stream: true`, multi-turn follow-up → confirm the rewrite step still
     resolves history correctly (it's unaffected by streaming, but verify
     the combination works end-to-end).
   - Confirm `chat_messages` has the correct 2 new rows after a streamed
     turn, matching the full accumulated text exactly.
   - `stream: true` with `ANSWER_PROVIDER=anthropic` (temporarily, no key
     configured) → confirm a normal JSON `500`, not a broken/hung stream.
2. **Extend `tests/smoke_test.py`** with the above as automated checks.
3. **Live verification against the deployed Railway instance** after
   deploying, matching this project's established practice.

## Files touched

| File | Change |
|---|---|
| `llm_provider.py` | `stream_complete` on `_OpenAICompatibleProvider` (covers `GroqProvider`/`OpenAIProvider`) and `GeminiProvider`. `AnthropicProvider` untouched. |
| `rag_engine.py` | New `generate_answer_stream()` alongside existing `generate_answer()`. |
| `app.py` | `ChatRequest` gains `stream: bool = False`. `/chat` handler branches on it; streaming branch uses `StreamingResponse`. |
| `tests/smoke_test.py` | New checks per the Testing plan above. |
| `API_CONTRACT.md` | Document the `stream` field and the SSE event schema on `/chat`. |
| `FRONTEND_INTEGRATION.md` | Add streaming as a feature, with a reference client example (SSE consumption via `fetch` + `ReadableStream`, since `EventSource` doesn't support `POST`). |
