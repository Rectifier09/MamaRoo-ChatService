# Streaming Responses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `stream: bool` field to `POST /chat` that returns the
answer as Server-Sent Events instead of one JSON body, with zero change to
the existing (non-streaming) response for any client that doesn't ask for it.

**Architecture:** Add `stream_complete()` (a chunk-yielding generator method)
to `GeminiProvider` and the shared `_OpenAICompatibleProvider` base (covering
`GroqProvider`/`OpenAIProvider`); add `generate_answer_stream()` to
`rag_engine.py` mirroring `generate_answer()`'s rewrite→embed→cache→retrieve
flow but yielding `("delta", text)` / `("done", dict)` tuples; wire `/chat`
in `app.py` to branch on `req.stream`, peeking the first yielded item
synchronously so a failure with zero content sent still returns a normal
JSON error rather than a broken stream.

**Tech Stack:** FastAPI `StreamingResponse`, Server-Sent Events (`text/event-stream`),
Python generators — no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-13-streaming-responses-design.md`

## Global Constraints

- `stream` defaults to `False`; when absent or `False`, `/chat`'s behavior
  and response are byte-for-byte identical to today. This is non-negotiable
  — an external developer is already building against the current contract.
- Only `GeminiProvider` and `_OpenAICompatibleProvider` (Groq/OpenAI) get
  `stream_complete()`. `AnthropicProvider` gets nothing added — not even a
  stub.
- `rewrite.py` never streams — it's called once, synchronously, before any
  streaming begins, exactly as today.
- A cache hit streams as exactly one `delta` event containing the full
  cached text, immediately followed by `done` — no artificial pacing.
- `chat_messages` and `qa_cache` are written only after the full answer is
  known and successful — identical timing/content to the non-streaming path,
  just triggered from inside the streaming generator instead of the plain
  handler body. A mid-stream error skips both writes entirely.
- SSE wire format: `data: {json}\n\n` per event, two payload shapes only —
  `{"delta": "<text>"}` and `{"done": true, ...}` (with either
  `session_id`/`sources`/`cached` on success or `error` on failure).
- A failure with **zero content ever sent to the client** — auth,
  validation, rewrite/retrieval/cache-lookup errors, an unsupported
  `ANSWER_PROVIDER`, or the LLM failing on its very first chunk — must
  return a normal JSON error response (matching today's non-streaming error
  shapes/status codes), never a broken/empty stream. Only a failure *after*
  at least one real `delta` was already queued to send becomes an in-stream
  `error` event, because by then the `200 text/event-stream` status is
  already committed.

---

### Task 1: `stream_complete()` on `_OpenAICompatibleProvider` (Groq + OpenAI)

**Files:**
- Modify: `llm_provider.py:96-104` (add a new method to the existing class)

**Interfaces:**
- Produces: `_OpenAICompatibleProvider.stream_complete(system: list[SystemBlock], messages: list[dict], model: str, max_tokens: int) -> Iterator[str]` — inherited by `GroqProvider` and `OpenAIProvider` automatically (no changes needed to either subclass).

- [ ] **Step 1: Add the streaming method**

In `llm_provider.py`, immediately after the existing `complete` method on
`_OpenAICompatibleProvider` (currently lines 96-104:
```python
    def complete(self, system, messages, model, max_tokens):
        system_text = "\n\n".join(block.text for block in system)
        full_messages = [{"role": "system", "content": system_text}, *messages]
        response = self._client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,  # `max_tokens` is deprecated on this endpoint
            messages=full_messages,
        )
        return response.choices[0].message.content or ""
```
), insert:

```python

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
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
```

- [ ] **Step 2: Verify by hand — real chunks arrive over time, not all at once**

```bash
source .venv/bin/activate
python3 -c "
import time
from llm_provider import get_provider, SystemBlock

provider = get_provider('groq')
start = time.time()
for chunk in provider.stream_complete(
    system=[SystemBlock(text='You are a helpful assistant. Answer in 3-4 sentences.')],
    messages=[{'role': 'user', 'content': 'Explain what a semantic cache is.'}],
    model='openai/gpt-oss-120b',
    max_tokens=200,
):
    print(f'[{time.time()-start:.2f}s] {chunk!r}')
"
```

Expected: multiple lines, each a short text fragment, with **increasing**
timestamps (not all printed at `[0.00s]`) — that's the proof this is real
incremental streaming and not a buffered-then-chunked fake. If every line
prints at the same instant, something is wrong (e.g. `stream=True` isn't
actually being honored) — stop and investigate before moving on.

- [ ] **Step 3: Commit**

```bash
git add llm_provider.py
git commit -m "Add stream_complete to _OpenAICompatibleProvider (Groq/OpenAI)"
```

---

### Task 2: `stream_complete()` on `GeminiProvider`

**Files:**
- Modify: `llm_provider.py:140-168` (add a new method to the existing class)

**Interfaces:**
- Produces: `GeminiProvider.stream_complete(system, messages, model, max_tokens) -> Iterator[str]`

- [ ] **Step 1: Add the streaming method**

In `llm_provider.py`, immediately after `GeminiProvider`'s existing
`complete` method (currently ends at line 168 with `return response.text or
""`), insert:

```python

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
```

- [ ] **Step 2: Verify by hand — real chunks arrive over time**

```bash
source .venv/bin/activate
python3 -c "
import time
from llm_provider import get_provider, SystemBlock

provider = get_provider('gemini')
start = time.time()
for chunk in provider.stream_complete(
    system=[SystemBlock(text='You are a helpful assistant. Answer in 3-4 sentences.')],
    messages=[{'role': 'user', 'content': 'Explain what a semantic cache is.'}],
    model='gemini-flash-latest',
    max_tokens=200,
):
    print(f'[{time.time()-start:.2f}s] {chunk!r}')
"
```

Expected: same as Task 1 Step 2 — multiple chunks, increasing timestamps.

- [ ] **Step 3: Commit**

```bash
git add llm_provider.py
git commit -m "Add stream_complete to GeminiProvider"
```

---

### Task 3: `generate_answer_stream()` in `rag_engine.py`

**Files:**
- Modify: `rag_engine.py:44-73` (add a new function alongside the existing `generate_answer`)

**Interfaces:**
- Consumes: `cache.find_cached_answer`, `cache.store_answer`, `embed_texts`, `rewrite_query`, `get_provider`, `SystemBlock`, `retrieve`, `_build_context`, `SYSTEM_INSTRUCTIONS` (all already defined in this file), `provider.stream_complete(...)` from Tasks 1-2.
- Produces:
  - `generate_answer_stream(message: str, history: list[dict] | None = None) -> Iterator[tuple[str, str] | tuple[str, dict]]` — yields `("delta", str)` zero or more times, then exactly one `("done", dict)`. The `dict` is `{"sources": list[str], "cached": bool}` on success or `{"error": str}` on a mid-generation failure. Nothing is yielded before rewrite/embed/cache-lookup/retrieve complete — an exception in any of those propagates as a normal Python exception out of the generator's first `next()` call (deliberately *not* caught here, so `app.py` can turn it into a plain JSON error — see Task 4).
  - `answer_provider_supports_streaming() -> bool` — `True` iff `get_provider(config.ANSWER_PROVIDER)` has a `stream_complete` method. `app.py` calls this *before* calling `generate_answer_stream` at all, since generators are lazy and an `AttributeError` from a missing method wouldn't otherwise surface until the client already has a committed `200 text/event-stream` response — see the spec's explicit note on this.

- [ ] **Step 1: Add both functions**

In `rag_engine.py`, immediately after the existing `generate_answer`
function (which currently ends at line 72 with `return answer, sources,
False`), insert:

```python


def answer_provider_supports_streaming() -> bool:
    return hasattr(get_provider(config.ANSWER_PROVIDER), "stream_complete")


def generate_answer_stream(message: str, history: list[dict] | None = None):
    """Yields ("delta", str) tuples as answer text becomes available, then
    exactly one ("done", dict): {"sources": [...], "cached": bool} on
    success, or {"error": str} if generation failed after streaming began.
    Exceptions from rewrite/embed/cache-lookup/retrieve are NOT caught here
    -- they propagate out of the generator's first next() call so the caller
    can distinguish "failed before anything was sent" from "failed mid-
    stream" (see app.py)."""
    canonical = rewrite_query(message, history)
    query_embedding = embed_texts([canonical])[0]

    cached = cache.find_cached_answer(query_embedding)
    if cached:
        yield "delta", cached["answer"]
        yield "done", {"sources": list(cached["sources"]), "cached": True}
        return

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
    try:
        for delta in provider.stream_complete(
            system=system,
            messages=messages,
            model=config.ANSWER_MODEL,
            max_tokens=config.MAX_TOKENS,
        ):
            full_answer += delta
            yield "delta", delta
    except Exception as exc:
        yield "done", {"error": str(exc)}
        return

    cache.store_answer(canonical, query_embedding, full_answer, sources)
    yield "done", {"sources": sources, "cached": False}
```

- [ ] **Step 2: Verify by hand — cache miss, then cache hit, on the real DB**

```bash
source .venv/bin/activate
python3 -c "
import rag_engine

print('--- cache miss ---')
for kind, payload in rag_engine.generate_answer_stream('What is Braxton Hicks?', history=[]):
    print(kind, repr(payload)[:80])

print()
print('--- cache hit (same question again) ---')
for kind, payload in rag_engine.generate_answer_stream('What is Braxton Hicks?', history=[]):
    print(kind, repr(payload)[:80])
"
python3 -c "
import db
db.execute('DELETE FROM qa_cache')
print('cleaned up qa_cache')
"
```

Expected: the first block prints several `delta` lines (the answer arriving
in pieces) followed by one `done ({'sources': [...], 'cached': False})`
line. The second block prints **exactly one** `delta` line (the full cached
answer, not chunked) followed by `done ({'sources': [...], 'cached': True})`.

Also verify the streaming-unsupported check:
```bash
python3 -c "
import config
config.ANSWER_PROVIDER = 'anthropic'
import rag_engine
print(rag_engine.answer_provider_supports_streaming())
"
```
Expected: `False` (since `AnthropicProvider` has no `stream_complete`).

- [ ] **Step 3: Commit**

```bash
git add rag_engine.py
git commit -m "Add generate_answer_stream and answer_provider_supports_streaming"
```

---

### Task 4: Wire `stream` into `POST /chat`

**Files:**
- Modify: `app.py:9` (import), `app.py:32-36` (`ChatRequest`), `app.py:90-125` (`/chat` handler + new helper)

**Interfaces:**
- Consumes: `rag_engine.generate_answer_stream`, `rag_engine.answer_provider_supports_streaming` (Task 3).
- Produces: the updated `/chat` route — Task 5's tests call it by URL, not by importing anything new.

- [ ] **Step 1: Import `StreamingResponse` and `json`**

In `app.py`, line 9 currently reads:
```python
from fastapi import Depends, FastAPI, Header, HTTPException, Query
```
Add two lines right after it:
```python
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
import json
```
(`import json` goes with the other stdlib imports at the top of the file —
place it on its own line after `import secrets` at line 5, not inline here;
i.e. line 5 becomes:
```python
import json
import secrets
```
alphabetized with the existing `import secrets`.)

- [ ] **Step 2: Add `stream` to `ChatRequest`**

`app.py`'s `ChatRequest` currently reads (lines 32-36):
```python
class ChatRequest(BaseModel):
    message: str
    end_user_id: str          # a stable id for this end user — real user id, or a
                               # client-generated UUID stored in localStorage
    session_id: Optional[int] = None  # omit to start a new conversation
```
Change to:
```python
class ChatRequest(BaseModel):
    message: str
    end_user_id: str          # a stable id for this end user — real user id, or a
                               # client-generated UUID stored in localStorage
    session_id: Optional[int] = None  # omit to start a new conversation
    stream: bool = False      # true -> text/event-stream instead of one JSON body
```

- [ ] **Step 3: Branch the handler and add the streaming helper**

`app.py`'s `/chat` handler currently reads (lines 90-125):
```python
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, product: dict = Depends(get_product)):
    rate_limit.check(product["api_key"], product["rate_limit_per_minute"])

    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    session_id = req.session_id
    if session_id is None:
        row = db.fetchone(
            "INSERT INTO chat_sessions (product_id, end_user_id) VALUES (%s, %s) RETURNING id",
            (product["id"], req.end_user_id),
        )
        session_id = row["id"]

    history_rows = db.fetchall(
        "SELECT role, content FROM chat_messages WHERE session_id = %s ORDER BY id DESC LIMIT %s",
        (session_id, config.MAX_HISTORY_TURNS * 2),
    )
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(history_rows)]

    try:
        answer, sources, cached = rag_engine.generate_answer(req.message, history=history)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'user', %s)",
        (session_id, req.message),
    )
    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
        (session_id, answer),
    )

    return ChatResponse(answer=answer, sources=sources, session_id=session_id, cached=cached)
```

Replace it with (the non-streaming branch below the new `if req.stream:`
early-return is character-for-character unchanged from today -- nothing
about its behavior changes, it just now runs only when `stream` is falsy):

```python
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, product: dict = Depends(get_product)):
    rate_limit.check(product["api_key"], product["rate_limit_per_minute"])

    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    session_id = req.session_id
    if session_id is None:
        row = db.fetchone(
            "INSERT INTO chat_sessions (product_id, end_user_id) VALUES (%s, %s) RETURNING id",
            (product["id"], req.end_user_id),
        )
        session_id = row["id"]

    history_rows = db.fetchall(
        "SELECT role, content FROM chat_messages WHERE session_id = %s ORDER BY id DESC LIMIT %s",
        (session_id, config.MAX_HISTORY_TURNS * 2),
    )
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(history_rows)]

    if req.stream:
        return _stream_chat_response(req, session_id, history)

    try:
        answer, sources, cached = rag_engine.generate_answer(req.message, history=history)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'user', %s)",
        (session_id, req.message),
    )
    db.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
        (session_id, answer),
    )

    return ChatResponse(answer=answer, sources=sources, session_id=session_id, cached=cached)


def _stream_chat_response(req: ChatRequest, session_id: int, history: list[dict]) -> StreamingResponse:
    if not rag_engine.answer_provider_supports_streaming():
        raise HTTPException(
            status_code=500,
            detail=f"Provider '{config.ANSWER_PROVIDER}' does not support streaming",
        )

    gen = rag_engine.generate_answer_stream(req.message, history=history)
    try:
        first_kind, first_payload = next(gen)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if first_kind == "done":
        # The LLM failed on its very first chunk -- nothing was ever sent to
        # the client yet, so this is still a normal JSON error, not a stream.
        raise HTTPException(status_code=500, detail=first_payload.get("error", "generation failed"))

    def event_stream():
        full_answer = first_payload
        yield f"data: {json.dumps({'delta': first_payload})}\n\n"
        for kind, payload in gen:
            if kind == "delta":
                full_answer += payload
                yield f"data: {json.dumps({'delta': payload})}\n\n"
            else:  # kind == "done"
                if "error" in payload:
                    yield f"data: {json.dumps({'done': True, 'error': payload['error']})}\n\n"
                    return
                db.execute(
                    "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'user', %s)",
                    (session_id, req.message),
                )
                db.execute(
                    "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
                    (session_id, full_answer),
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "done": True,
                            "session_id": session_id,
                            "sources": payload["sources"],
                            "cached": payload["cached"],
                        }
                    )
                    + "\n\n"
                )

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )
```

- [ ] **Step 4: Verify by hand — regression check, then real streaming**

```bash
source .venv/bin/activate
uvicorn app:app --port 8000 &
sleep 3
API_KEY="pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF"
```

Regression check — `stream` omitted, and explicitly `false`, both still give
the exact non-streaming JSON shape:
```bash
curl -s -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"plan-verify-stream-regress","session_id":null}'
echo
curl -s -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"plan-verify-stream-regress","stream":false}'
```
Expected: both return the same `{"answer": ..., "sources": [...],
"session_id": ..., "cached": ...}` shape as before this task — no `stream`
key anywhere in the response, nothing about the JSON path changed.

Real streaming, watched live (curl's own output buffering must be disabled
with `-N`, or you won't see the incremental arrival even though the server
is sending it correctly):
```bash
curl -N -s -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What foods should I avoid during pregnancy?","end_user_id":"plan-verify-stream","stream":true}'
```
Expected: several `data: {"delta": "..."}` lines printed to the terminal
**one at a time as they arrive** (visibly, not all at once — watch it run,
don't just check the final output), ending with one `data: {"done": true,
"session_id": ..., "sources": [...], "cached": false}` line.

Cache-hit-while-streaming (repeat the same question):
```bash
curl -N -s -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What foods should I avoid during pregnancy?","end_user_id":"plan-verify-stream","stream":true}'
```
Expected: **exactly one** `data: {"delta": ...}` line (the whole cached
answer at once), then `data: {"done": true, ..., "cached": true}`.

Confirm the DB actually got the streamed turn's messages:
```bash
python3 -c "
import db
rows = db.fetchall(\"SELECT role, content FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = 'plan-verify-stream') ORDER BY id\")
for r in rows: print(r['role'], '-', r['content'][:60])
"
```
Expected: 4 rows (2 turns × user+assistant — the cache-miss turn and the
cache-hit turn), assistant content matching what streamed.

Unsupported-provider path — stop this server, start a fresh one with
`ANSWER_PROVIDER=anthropic` (no key configured) to confirm the *synchronous*
pre-check works, not a hung/broken stream:
```bash
kill %1
ANSWER_PROVIDER=anthropic nohup uvicorn app:app --port 8000 > /tmp/plan-verify-anthropic.log 2>&1 &
sleep 3
curl -s -w "\nHTTP:%{http_code}\n" -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"plan-verify-stream-unsupported","stream":true}'
kill %1
```
Expected: a normal JSON body `{"detail":"Provider 'anthropic' does not
support streaming"}` with `HTTP:500` — **not** an empty/hung `200
text/event-stream` response. This is the exact case the spec's synchronous
pre-check exists for.

Clean up test data:
```bash
source .venv/bin/activate
uvicorn app:app --port 8000 &
sleep 3
python3 -c "
import db
for user in ('plan-verify-stream-regress', 'plan-verify-stream', 'plan-verify-stream-unsupported'):
    db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = %s)\", (user,))
    db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = %s\", (user,))
db.execute('DELETE FROM qa_cache')
print('cleaned up')
"
kill %1
```

- [ ] **Step 5: Commit**

```bash
git add app.py
git commit -m "Add opt-in streaming (stream field) to POST /chat"
```

---

### Task 5: Automated regression coverage in `tests/smoke_test.py`

**Files:**
- Modify: `tests/smoke_test.py:20-27` (add `import json`), `tests/smoke_test.py:73-95` (add a streaming request helper near the existing `chat`/`retry_on_transient_503` helpers), `tests/smoke_test.py:250-267` (insert new checks between the existing empty-session-exclusion block and cleanup)

**Interfaces:**
- Consumes: `BASE_URL`, `main_key`, `REAL_QUESTION`, `_created_end_users`, `check`, `db` (all already defined earlier in this file).
- Produces: `chat_stream(api_key, message, end_user_id, session_id=None) -> tuple[list[dict], int]` — returns `(events, status_code)`, where `events` is every parsed `data:` JSON payload in order (empty list if the response wasn't actually a stream, e.g. a `500` pre-stream error).

- [ ] **Step 1: Add the `json` import**

`tests/smoke_test.py` currently starts (lines 20-23):
```python
import sys
import time

import httpx
```
Change to:
```python
import json
import sys
import time

import httpx
```

- [ ] **Step 2: Add the streaming request helper**

Immediately after the existing `retry_on_transient_503` function (currently
lines 86-95:
```python
def retry_on_transient_503(fn, attempts: int = 3, delay: float = 8.0):
    """Gemini's free tier returns transient 503s under load — not a bug in this
    service. Retry a few times before treating a 503 as a real failure."""
    resp = fn()
    tries = 1
    while resp.status_code == 500 and "UNAVAILABLE" in resp.text and tries < attempts:
        time.sleep(delay)
        resp = fn()
        tries += 1
    return resp
```
), insert:

```python


def chat_stream(api_key: str, message: str, end_user_id: str, session_id: int | None = None):
    """POSTs to /chat with stream=true and reads the SSE response. Returns
    (events, status_code) -- events is every parsed `data:` JSON payload in
    order; empty if the response was a pre-stream JSON error instead."""
    _created_end_users.append(end_user_id)
    events = []
    with httpx.stream(
        "POST",
        f"{BASE_URL}/chat",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={
            "message": message,
            "end_user_id": end_user_id,
            "session_id": session_id,
            "stream": True,
        },
        timeout=60,
    ) as resp:
        status_code = resp.status_code
        if status_code != 200:
            resp.read()
            return events, status_code
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events, status_code


def retry_on_transient_503_stream(fn, attempts: int = 3, delay: float = 8.0):
    """Same retry logic as retry_on_transient_503, adapted for chat_stream's
    (events, status_code) return shape instead of an httpx.Response."""
    events, status = fn()
    tries = 1
    while status == 500 and any("UNAVAILABLE" in str(e) for e in events) and tries < attempts:
        time.sleep(delay)
        events, status = fn()
        tries += 1
    return events, status
```

Note: this file does not add an automated check for the "unsupported
`ANSWER_PROVIDER`" case (Task 4's manual verification covers it with a
temporary `ANSWER_PROVIDER=anthropic` server). `tests/smoke_test.py` runs
against an already-running, already-configured instance (local or live) and
has no mechanism to override that instance's environment per-request — none
of the existing checks in this file do that either. Spinning up a second
server process with different env vars specifically for one automated check
would be new test-infrastructure scope beyond what this file does anywhere
else; the manual verification in Task 4 is sufficient given how rarely
`ANSWER_PROVIDER` changes.

- [ ] **Step 3: Insert the new checks**

Immediately after the existing empty-session-exclusion block (currently
lines 250-267:
```python
    # --- empty-session exclusion ---
    empty_user = "smoke-empty-session"
    _created_end_users.append(empty_user)
    db.fetchone(
        "INSERT INTO chat_sessions (product_id, end_user_id) VALUES (%s, %s) RETURNING id",
        (main_product["id"], empty_user),
    )
    empty_list_resp = httpx.get(
        f"{BASE_URL}/chat/sessions",
        headers={"X-API-Key": main_key},
        params={"end_user_id": empty_user},
        timeout=10,
    )
    check(
        "GET /chat/sessions excludes sessions with zero messages",
        empty_list_resp.status_code == 200 and empty_list_resp.json().get("sessions") == [],
        empty_list_resp.text,
    )
```
) and before the `# --- cleanup ---` comment, insert:

```python

    # --- streaming: regression check, stream explicitly false ---
    regress_resp = httpx.post(
        f"{BASE_URL}/chat",
        headers={"X-API-Key": main_key, "Content-Type": "application/json"},
        json={"message": "hi", "end_user_id": "smoke-stream-regress", "session_id": None, "stream": False},
        timeout=30,
    )
    _created_end_users.append("smoke-stream-regress")
    check(
        "POST /chat stream=false -> normal JSON shape unchanged",
        regress_resp.status_code == 200 and "answer" in regress_resp.json() and "delta" not in regress_resp.text,
        regress_resp.text,
    )

    # --- streaming: cache miss ---
    stream_user = "smoke-stream"
    events, status = retry_on_transient_503_stream(
        lambda: chat_stream(main_key, REAL_QUESTION, stream_user)
    )
    check("POST /chat stream=true cache miss -> 200", status == 200, str(events))
    deltas = [e["delta"] for e in events if "delta" in e]
    done_events = [e for e in events if e.get("done")]
    check("stream cache miss: at least one delta event", len(deltas) >= 1, str(events))
    check(
        "stream cache miss: exactly one done event, cached=false, real sources",
        len(done_events) == 1
        and done_events[0].get("cached") is False
        and len(done_events[0].get("sources", [])) > 0,
        str(done_events),
    )
    stream_session_id = done_events[0].get("session_id") if done_events else None
    full_streamed_answer = "".join(deltas)

    # --- streaming: cache hit (same question again) -> exactly one delta ---
    events2, status2 = chat_stream(main_key, REAL_QUESTION, stream_user)
    check("POST /chat stream=true cache hit -> 200", status2 == 200, str(events2))
    deltas2 = [e["delta"] for e in events2 if "delta" in e]
    done_events2 = [e for e in events2 if e.get("done")]
    check(
        "stream cache hit: exactly one delta event (whole cached answer, not chunked)",
        len(deltas2) == 1,
        str(events2),
    )
    check(
        "stream cache hit: done event has cached=true",
        len(done_events2) == 1 and done_events2[0].get("cached") is True,
        str(done_events2),
    )

    # --- streaming: multi-turn follow-up still resolves via rewrite ---
    events3, status3 = retry_on_transient_503_stream(
        lambda: chat_stream(main_key, FOLLOWUP, stream_user, session_id=stream_session_id)
    )
    check("POST /chat stream=true multi-turn -> 200", status3 == 200, str(events3))
    followup_answer = "".join(e["delta"] for e in events3 if "delta" in e).lower()
    check(
        "stream multi-turn follow-up resolves against history",
        any(kw in followup_answer for kw in ("afternoon", "evening", "exercise", "due date")),
        followup_answer,
    )

    # --- streaming: DB has the correct persisted messages ---
    persisted = db.fetchall(
        "SELECT role, content FROM chat_messages WHERE session_id = %s ORDER BY id",
        (stream_session_id,),
    )
    check(
        "streamed turn persisted correctly to chat_messages",
        len(persisted) >= 2
        and persisted[0]["role"] == "user"
        and persisted[0]["content"] == REAL_QUESTION
        and persisted[1]["role"] == "assistant"
        and persisted[1]["content"] == full_streamed_answer,
        str(persisted[:2]),
    )
```

- [ ] **Step 4: Run the full smoke test locally and verify all checks pass**

```bash
source .venv/bin/activate
uvicorn app:app --port 8000 &
sleep 3
python tests/smoke_test.py http://localhost:8000
```

Expected: `All checks passed.` — the full pre-existing suite (21 checks as
of the last shipped feature) plus all the new streaming checks added here.
If a new check fails, fix the Task 1-4 code, not the test, unless the test
itself has a bug — re-run until clean.

```bash
kill %1
```

- [ ] **Step 5: Commit**

```bash
git add tests/smoke_test.py
git commit -m "Add smoke test coverage for streaming responses"
```

---

### Task 6: Document streaming

**Files:**
- Modify: `API_CONTRACT.md` (extend the existing `POST /chat` section)
- Modify: `FRONTEND_INTEGRATION.md` (feature list, Quick facts table, new
  reference client function)

**Interfaces:** None — documentation only, no code.

- [ ] **Step 1: Document `stream` in `API_CONTRACT.md`**

In `API_CONTRACT.md`, in the `POST /chat` section, the request-body field
list currently ends with (see the existing `session_id` bullet at line 39):
```markdown
- `session_id` (integer, optional) — omit or send `null` to start a new conversation.
  Send back the `session_id` from a prior response to continue that conversation
  (the server loads recent history itself — the client does not resend prior turns).
```
Add immediately after it:
```markdown
- `stream` (boolean, optional, default `false`) — when `true`, the response
  is `text/event-stream` instead of a single JSON body (see below). When
  absent or `false`, behavior is unchanged from the rest of this section.
```

Then, immediately after the existing `**Error responses:**` table for
`/chat` (the one ending with the `500` row, right before the `##
POST /admin/products` heading), insert a new subsection:

```markdown
### `POST /chat` with `stream: true`

Same request shape, `stream: true`. Response is `200 text/event-stream`.
Each line is `data: <json>\n\n`. Two payload shapes:

**Delta** (zero or more, as text becomes available):
```json
{ "delta": "Braxton Hicks are" }
```
For a cache hit, exactly **one** delta event is sent, containing the entire
cached answer.

**Terminal** (exactly one, always last):
```json
{ "done": true, "session_id": 42, "sources": ["Mayo-Clinic-Pregnancy-Library.md"], "cached": false }
```
or, only if generation failed *after* at least one delta was already sent:
```json
{ "done": true, "error": "<message>" }
```

**Errors that happen before any content was sent** (auth, validation,
retrieval/cache-lookup failure, an `ANSWER_PROVIDER` that doesn't support
streaming, or the LLM failing on its very first chunk) return a normal JSON
error response — same status codes and body shape as the non-streaming
`/chat` error table above, not a stream. Only a failure *after* streaming
has genuinely started becomes the in-stream `error` payload described above,
since the `200 text/event-stream` status is already committed by then.
```

- [ ] **Step 2: Document streaming in `FRONTEND_INTEGRATION.md`**

In the "Quick facts" table, the `**Streaming**` row currently reads:
```markdown
| **Streaming** | Not supported — you get the full answer in one response |
```
Change to:
```markdown
| **Streaming** | Supported, opt-in — set `stream: true` on the request (see below) |
```

In the "Features this API supports" list, add a new bullet after the
existing "Response caching" bullet:
```markdown
- **Streaming** — set `stream: true` on the request and the response
  becomes Server-Sent Events instead of one JSON body, so you can render the
  answer as it's generated instead of waiting for the whole thing. Opt-in:
  omit `stream` (or send `false`) and nothing changes from the default
  behavior described above.
```

In "What it does *not* support", remove the line:
```markdown
- **No streaming** — don't build a token-by-token typing effect; show a
  loading state until the full response arrives.
```

At the end of the "Reference implementation" code block, after the existing
`openConversation()` function (added by the previous feature), add:

```js
async function sendMessageStreaming(text, { onDelta, onDone } = {}) {
  const sessionId = localStorage.getItem("mamaroo_session_id");
  const res = await fetch(CHAT_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": CHAT_API_KEY },
    body: JSON.stringify({
      message: text,
      end_user_id: getEndUserId(),
      session_id: sessionId ? Number(sessionId) : null,
      stream: true,
    }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(`Chat request failed (${res.status}): ${body.detail ?? "unknown error"}`);
  }

  // EventSource doesn't support POST, so streaming is read manually via the
  // response body's ReadableStream instead.
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const line = rawEvent.startsWith("data: ") ? rawEvent.slice(6) : rawEvent;
      if (!line) continue;
      const payload = JSON.parse(line);

      if (payload.delta !== undefined) {
        onDelta?.(payload.delta);
      } else if (payload.done) {
        if (payload.error) throw new Error(`Streaming failed: ${payload.error}`);
        localStorage.setItem("mamaroo_session_id", String(payload.session_id));
        onDone?.(payload); // { session_id, sources, cached }
      }
    }
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add API_CONTRACT.md FRONTEND_INTEGRATION.md
git commit -m "Document streaming (stream field, SSE event schema, reference client)"
```

---

### Task 7: Deploy and verify live

**Files:** None (deploy + verification only).

**Interfaces:** None.

- [ ] **Step 1: Push to trigger the Railway auto-deploy**

```bash
git push origin main
```

- [ ] **Step 2: Wait for the deploy to succeed**

Poll with the Railway MCP tools (`list-deployments` for project id
`b5d5a575-6ea3-4e92-980d-9905f84fe872`, service id
`be2022c9-aa47-45d2-b131-cdf7a0073218`) until the latest deployment's
`status` is `SUCCESS`. Don't test against the live URL before this — the
previous deployment keeps serving traffic during the build/health-check
window (confirmed earlier this session).

- [ ] **Step 3: Run the smoke test against the live URL**

```bash
source .venv/bin/activate
python tests/smoke_test.py https://app-production-cc74.up.railway.app
```

Expected: `All checks passed.` If an LLM-dependent check fails with a
`RESOURCE_EXHAUSTED`/`429`, that's a quota issue on whichever provider is
currently configured — not a code issue (see the Decisions log); re-run
later rather than debugging it as a bug in this feature.

- [ ] **Step 4: Confirm streaming is genuinely incremental over the live network, not proxy-buffered**

This matters specifically for the live URL: Railway's edge sits in front of
the app (Cloudflare-fronted, per response headers seen earlier this
session), and an intermediate proxy *could* buffer the whole SSE response
before forwarding it, which would still pass every correctness check above
(same final content, same event shapes) while silently defeating the entire
point of this feature — the user would wait just as long as before, just
without knowing it.

```bash
curl -N -s -w "\n" -X POST https://app-production-cc74.up.railway.app/chat \
  -H "X-API-Key: pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF" -H "Content-Type: application/json" \
  -d '{"message":"What vaccines are recommended during pregnancy?","end_user_id":"plan-verify-live-stream","stream":true}' \
  | while IFS= read -r line; do echo "$(date +%H:%M:%S.%3N) $line"; done
```

Expected: the printed timestamps on successive `data:` lines are **visibly
different** (spread over at least a couple of seconds for a multi-sentence
answer), not all identical/near-identical. If every line has the same
timestamp, streaming is being buffered somewhere between Railway's edge and
the client — stop and investigate (this would likely need a header change
like disabling proxy buffering, or confirming `X-Accel-Buffering`/Cloudflare
settings, which is a new finding to report, not something to silently work
around) rather than declaring the feature done.

Clean up the test conversation afterward:
```bash
source .venv/bin/activate
python3 -c "
import db
db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = 'plan-verify-live-stream')\")
db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = 'plan-verify-live-stream'\")
db.execute('DELETE FROM qa_cache')
"
```

- [ ] **Step 5: Report the result**

State plainly whether the full smoke test passed against the live URL
(paste the final `N failure(s).` / `All checks passed.` line) and whether
streaming was confirmed genuinely incremental over the real network (paste
the timestamped output from Step 4) — not just "it looks deployed."
