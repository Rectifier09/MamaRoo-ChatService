# Conversation Thread Browsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two read-only endpoints — `GET /chat/sessions` (list a user's past
conversations) and `GET /chat/sessions/{session_id}/messages` (fetch one
conversation's full history) — so a client can rediscover and reopen past
conversations after losing its stored `session_id`.

**Architecture:** Two new FastAPI route handlers in `app.py`, backed entirely by
the existing `chat_sessions`/`chat_messages` tables via raw SQL through the
existing `db.fetchone`/`db.fetchall` helpers — no schema changes, no new files.
Both reuse the existing `auth.get_product` dependency for identical API-key +
origin enforcement to `/chat`.

**Tech Stack:** FastAPI, Pydantic, psycopg (via `db.py`) — all already in use,
no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-13-conversation-thread-browsing-design.md`

## Global Constraints

- No database schema changes — `chat_sessions`/`chat_messages` already carry
  everything needed.
- Both endpoints scope every query to `(product_id from the API key,
  end_user_id from the query param)` — a product can never see another
  product's data, an end user can never see another end user's data.
- `GET /chat/sessions` excludes sessions with zero messages (failed-request
  artifacts — see spec's Problem section for why these exist).
- `GET /chat/sessions/{id}/messages` returns `404` for both "session doesn't
  exist" and "session exists but belongs to a different end user" — never
  distinguish the two (spec: avoids ID enumeration).
- `title` in the list response = the session's first user message, truncated
  to 80 chars with a trailing `…` if truncated.
- This project's established testing pattern (used throughout this session,
  including by the existing `tests/smoke_test.py`) is: implement, verify by
  hand against a real running instance and real Postgres (no mocking), *then*
  encode that verification as an automated regression check. Tasks below
  follow that same order rather than a literal red-green-refactor cycle,
  since there is no unit-test framework in this repo to write a failing test
  against — `tests/smoke_test.py` itself is the test harness, and it needs
  the endpoints to exist before it can call them.
- Follow this repo's existing style: raw parameterized SQL (no ORM), FastAPI
  `Depends(get_product)` for auth, Pydantic `BaseModel` response models.

---

### Task 1: `GET /chat/sessions` (list endpoint)

**Files:**
- Modify: `app.py:6` (imports), `app.py:53-56` (new models), `app.py:96-99`
  (new route)

**Interfaces:**
- Produces: `SessionSummary` (Pydantic model: `session_id: int`, `title: str`,
  `last_message_at: datetime`, `created_at: datetime`), `SessionListResponse`
  (`sessions: List[SessionSummary]`), and a module-level helper
  `_truncate_title(text: str, max_len: int = 80) -> str` — Task 2 does not
  need these, but Task 3's tests call `GET /chat/sessions` by URL, not by
  importing these names directly.

- [ ] **Step 1: Add the `datetime` import**

In `app.py`, line 6 currently reads:
```python
from typing import List, Optional
```
Change it to:
```python
from datetime import datetime
from typing import List, Optional
```

- [ ] **Step 2: Add `Query` to the fastapi import**

In `app.py`, line 8 currently reads:
```python
from fastapi import Depends, FastAPI, Header, HTTPException
```
Change it to:
```python
from fastapi import Depends, FastAPI, Header, HTTPException, Query
```

- [ ] **Step 3: Add the response models and title-truncation helper**

In `app.py`, immediately after the existing `CreateProductResponse` class
(currently lines 51-53:
```python
class CreateProductResponse(BaseModel):
    id: int
    api_key: str
```
) and before `@app.get("/health")` (currently line 56), insert:

```python
class SessionSummary(BaseModel):
    session_id: int
    title: str
    last_message_at: datetime
    created_at: datetime


class SessionListResponse(BaseModel):
    sessions: List[SessionSummary]


class MessageItem(BaseModel):
    role: str
    content: str
    created_at: datetime


class SessionMessagesResponse(BaseModel):
    session_id: int
    messages: List[MessageItem]


def _truncate_title(text: str, max_len: int = 80) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"
```

(`MessageItem`/`SessionMessagesResponse` are defined here too, in the same
block, even though Task 2 is what uses them — keeps all four response models
together rather than splitting model definitions across two tasks.)

- [ ] **Step 4: Add the list-sessions route**

`limit`/`offset` use FastAPI's `Query(...)` validators (`ge`/`le`) rather
than manually clamping out-of-range values — the spec says "max 100" but
doesn't say what should happen if a client sends more; rejecting with `422`
is more honest than silently returning fewer results than requested without
saying so.

In `app.py`, immediately after the `/chat` handler's `return` statement
(currently line 96: `return ChatResponse(answer=answer, sources=sources,
session_id=session_id, cached=cached)`) and before `@app.post("/admin/products"...)`
(currently line 99), insert:

```python
@app.get("/chat/sessions", response_model=SessionListResponse)
def list_sessions(
    end_user_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    product: dict = Depends(get_product),
):
    rows = db.fetchall(
        """
        SELECT
          s.id AS session_id,
          (SELECT content FROM chat_messages
           WHERE session_id = s.id AND role = 'user'
           ORDER BY id LIMIT 1) AS title_raw,
          (SELECT MAX(created_at) FROM chat_messages WHERE session_id = s.id) AS last_message_at,
          s.created_at
        FROM chat_sessions s
        WHERE s.product_id = %s
          AND s.end_user_id = %s
          AND EXISTS (SELECT 1 FROM chat_messages WHERE session_id = s.id)
        ORDER BY last_message_at DESC
        LIMIT %s OFFSET %s
        """,
        (product["id"], end_user_id, limit, offset),
    )
    sessions = [
        SessionSummary(
            session_id=r["session_id"],
            title=_truncate_title(r["title_raw"]),
            last_message_at=r["last_message_at"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return SessionListResponse(sessions=sessions)
```

- [ ] **Step 5: Run the app locally and verify by hand**

```bash
source .venv/bin/activate
uvicorn app:app --port 8000 &
sleep 3
```

Create two real conversations for one end user (these are real LLM calls —
each costs one Groq request; the knowledge base must already be ingested —
see `MamaRoo-ChatService-Setup-Guide` in the Vault if `kb_chunks` is empty):

```bash
API_KEY="pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF"   # the existing test product key

curl -s -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"plan-verify-list","session_id":null}'

curl -s -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What vaccines are recommended during pregnancy?","end_user_id":"plan-verify-list","session_id":null}'
```

Then list them:

```bash
curl -s "http://localhost:8000/chat/sessions?end_user_id=plan-verify-list" \
  -H "X-API-Key: $API_KEY" | python3 -m json.tool
```

Expected: `200`, a `sessions` array with **2** entries, the vaccines
conversation listed **first** (most recently active), each `title` matching
its first message verbatim (both are short enough here to not be truncated).

Also verify the empty-list case (a user with no conversations):
```bash
curl -s "http://localhost:8000/chat/sessions?end_user_id=plan-verify-nobody" \
  -H "X-API-Key: $API_KEY"
```
Expected: `200`, `{"sessions": []}`.

Then verify `end_user_id` is actually required:
```bash
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8000/chat/sessions" \
  -H "X-API-Key: $API_KEY"
```
Expected: `422`.

Clean up the two test conversations and stop the server:
```bash
python3 -c "
import db
db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = 'plan-verify-list')\")
db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = 'plan-verify-list'\")
db.execute('DELETE FROM qa_cache')
"
kill %1
```

- [ ] **Step 6: Commit**

```bash
git add app.py
git commit -m "Add GET /chat/sessions to list a user's past conversations"
```

---

### Task 2: `GET /chat/sessions/{session_id}/messages` (detail endpoint)

**Files:**
- Modify: `app.py` (new route, placed directly after Task 1's `list_sessions`
  route and before `@app.post("/admin/products"...)`)

**Interfaces:**
- Consumes: `MessageItem`, `SessionMessagesResponse` (defined in Task 1, Step 3)
- Produces: the `GET /chat/sessions/{session_id}/messages` route itself —
  Task 3's tests call it by URL.

- [ ] **Step 1: Add the session-detail route**

Immediately after the `list_sessions` function added in Task 1 (and still
before `@app.post("/admin/products"...)`), insert:

```python
@app.get("/chat/sessions/{session_id}/messages", response_model=SessionMessagesResponse)
def get_session_messages(
    session_id: int,
    end_user_id: str,
    product: dict = Depends(get_product),
):
    owned = db.fetchone(
        "SELECT id FROM chat_sessions WHERE id = %s AND product_id = %s AND end_user_id = %s",
        (session_id, product["id"], end_user_id),
    )
    if not owned:
        raise HTTPException(status_code=404, detail="Session not found")

    rows = db.fetchall(
        "SELECT role, content, created_at FROM chat_messages WHERE session_id = %s ORDER BY id",
        (session_id,),
    )
    messages = [
        MessageItem(role=r["role"], content=r["content"], created_at=r["created_at"])
        for r in rows
    ]
    return SessionMessagesResponse(session_id=session_id, messages=messages)
```

- [ ] **Step 2: Run the app locally and verify by hand**

```bash
source .venv/bin/activate
uvicorn app:app --port 8000 &
sleep 3
API_KEY="pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF"
```

Create one conversation, then fetch its full history:
```bash
SESSION_ID=$(curl -s -X POST http://localhost:8000/chat \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"plan-verify-detail","session_id":null}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['session_id'])")

curl -s "http://localhost:8000/chat/sessions/$SESSION_ID/messages?end_user_id=plan-verify-detail" \
  -H "X-API-Key: $API_KEY" | python3 -m json.tool
```
Expected: `200`, `messages` array with exactly 2 entries — `role: "user"`
(content = the question) then `role: "assistant"` (the real grounded answer).

Verify cross-user isolation — same session, wrong `end_user_id`:
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://localhost:8000/chat/sessions/$SESSION_ID/messages?end_user_id=someone-else" \
  -H "X-API-Key: $API_KEY"
```
Expected: `404`.

Verify a nonexistent session:
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "http://localhost:8000/chat/sessions/999999999/messages?end_user_id=plan-verify-detail" \
  -H "X-API-Key: $API_KEY"
```
Expected: `404`.

Clean up and stop the server:
```bash
python3 -c "
import db
db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = 'plan-verify-detail')\")
db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = 'plan-verify-detail'\")
db.execute('DELETE FROM qa_cache')
"
kill %1
```

- [ ] **Step 3: Commit**

```bash
git add app.py
git commit -m "Add GET /chat/sessions/{session_id}/messages to fetch one thread's history"
```

---

### Task 3: Automated regression coverage in `tests/smoke_test.py`

**Files:**
- Modify: `tests/smoke_test.py:148-156` (insert new checks between the
  existing rate-limiting block and the cleanup block)

**Interfaces:**
- Consumes: `chat()`, `retry_on_transient_503()`, `check()`, `REAL_QUESTION`,
  `_created_end_users` (all already defined earlier in this file — see the
  full current file content below for exact line numbers), plus the two live
  endpoints from Tasks 1-2 (`GET /chat/sessions`, `GET
  /chat/sessions/{id}/messages`).

- [ ] **Step 1: Insert the new checks**

In `tests/smoke_test.py`, immediately after the existing rate-limiting block
(currently lines 148-156:
```python
    # --- rate limiting ---
    r1 = chat(ratelimit_key, "ping one", "smoke-ratelimit")
    r2 = chat(ratelimit_key, "ping two", "smoke-ratelimit")
    r3 = chat(ratelimit_key, "ping three", "smoke-ratelimit")
    check(
        "rate limit enforced (3rd request in same minute -> 429)",
        r3.status_code == 429,
        f"r1={r1.status_code} r2={r2.status_code} r3={r3.status_code}",
    )
```
) and before the `# --- cleanup ---` comment (currently line 158), insert:

```python
    # --- session listing ---
    list_user = "smoke-sessions-list"
    resp1 = retry_on_transient_503(lambda: chat(main_key, REAL_QUESTION, list_user))
    check("session-list setup: first thread created", resp1.status_code == 200, resp1.text)
    first_session_id = resp1.json().get("session_id")

    resp2 = retry_on_transient_503(
        lambda: chat(main_key, "What vaccines are recommended during pregnancy?", list_user)
    )
    check("session-list setup: second thread created", resp2.status_code == 200, resp2.text)
    second_session_id = resp2.json().get("session_id")

    list_resp = httpx.get(
        f"{BASE_URL}/chat/sessions",
        headers={"X-API-Key": main_key},
        params={"end_user_id": list_user},
        timeout=10,
    )
    check("GET /chat/sessions -> 200", list_resp.status_code == 200, list_resp.text)
    sessions = list_resp.json().get("sessions", [])
    session_ids_returned = [s["session_id"] for s in sessions]
    check(
        "GET /chat/sessions returns both threads, most recent first",
        session_ids_returned[:2] == [second_session_id, first_session_id],
        str(session_ids_returned),
    )
    check(
        "GET /chat/sessions titles match each thread's first message",
        len(sessions) >= 2
        and sessions[0]["title"] == "What vaccines are recommended during pregnancy?"
        and sessions[1]["title"] == REAL_QUESTION,
        str(sessions[:2]),
    )

    # --- session detail ---
    detail_resp = httpx.get(
        f"{BASE_URL}/chat/sessions/{first_session_id}/messages",
        headers={"X-API-Key": main_key},
        params={"end_user_id": list_user},
        timeout=10,
    )
    check("GET /chat/sessions/{id}/messages -> 200", detail_resp.status_code == 200, detail_resp.text)
    detail_messages = detail_resp.json().get("messages", [])
    check(
        "session detail has 2 messages (user then assistant) in order",
        len(detail_messages) == 2
        and detail_messages[0]["role"] == "user"
        and detail_messages[0]["content"] == REAL_QUESTION
        and detail_messages[1]["role"] == "assistant",
        str(detail_messages),
    )

    # --- session cross-user isolation ---
    isolation_resp = httpx.get(
        f"{BASE_URL}/chat/sessions/{first_session_id}/messages",
        headers={"X-API-Key": main_key},
        params={"end_user_id": "smoke-different-user"},
        timeout=10,
    )
    check("session detail wrong end_user_id -> 404", isolation_resp.status_code == 404)

    missing_resp = httpx.get(
        f"{BASE_URL}/chat/sessions/999999999/messages",
        headers={"X-API-Key": main_key},
        params={"end_user_id": list_user},
        timeout=10,
    )
    check("session detail nonexistent session -> 404", missing_resp.status_code == 404)

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

- [ ] **Step 2: Update the file's module docstring**

The docstring (lines 1-15) currently ends with:
```python
Requires ADMIN_API_KEY in the environment (loaded from .env via config.py) —
used to provision throwaway test products, which are deleted again at the end.
"""
```
Change it to also mention the new coverage:
```python
Requires ADMIN_API_KEY in the environment (loaded from .env via config.py) —
used to provision throwaway test products, which are deleted again at the end.

Also covers GET /chat/sessions (list) and GET /chat/sessions/{id}/messages
(detail): thread listing/ordering/titles, cross-user isolation, and
zero-message-session exclusion.
"""
```

- [ ] **Step 3: Run the full smoke test locally and verify all checks pass**

```bash
source .venv/bin/activate
uvicorn app:app --port 8000 &
sleep 3
python tests/smoke_test.py http://localhost:8000
```

Expected: `All checks passed.` (every check from the pre-existing suite plus
all the new ones added in Step 1). If any of the new checks fail, fix the
route code from Task 1/2 (not the test) unless the test itself has a bug —
re-run until clean before moving on.

```bash
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add tests/smoke_test.py
git commit -m "Add smoke test coverage for conversation thread listing/detail"
```

---

### Task 4: Document the new endpoints

**Files:**
- Modify: `API_CONTRACT.md` (append two new endpoint sections, same style as
  the existing three)
- Modify: `FRONTEND_INTEGRATION.md` (add to the feature table and the
  endpoint reference)

**Interfaces:** None — documentation only, no code.

- [ ] **Step 1: Add both endpoints to `API_CONTRACT.md`**

Append, after the existing `POST /admin/products` section (at the end of the
file), matching the file's existing header/table style exactly:

```markdown
## `GET /chat/sessions`

Lists an end user's past conversations, most recently active first.

**Headers:**
| Header | Required | Notes |
|---|---|---|
| `X-API-Key` | Yes | Same as `/chat`. |

**Query params:**
| Param | Required | Notes |
|---|---|---|
| `end_user_id` | Yes | Same identifier used in `POST /chat`. |
| `limit` | No | Default 20, max 100. |
| `offset` | No | Default 0. |

**Response `200`:**
```json
{
  "sessions": [
    {
      "session_id": 42,
      "title": "What is Braxton Hicks?",
      "last_message_at": "2026-09-13T11:35:55Z",
      "created_at": "2026-09-13T11:35:38Z"
    }
  ]
}
```
- `title` — the session's first user message, truncated to 80 characters with
  a trailing `…` if truncated.
- Sessions with zero messages (e.g. from a failed `/chat` request) are never
  included.
- Empty `sessions: []` if the user has no conversations — not an error.

**Error responses:**
| Status | Condition |
|---|---|
| `401` | `X-API-Key` missing or invalid |
| `422` | `end_user_id` missing |

## `GET /chat/sessions/{session_id}/messages`

Fetches one conversation's full message history, in order.

**Headers:**
| Header | Required | Notes |
|---|---|---|
| `X-API-Key` | Yes | Same as `/chat`. |

**Query params:**
| Param | Required | Notes |
|---|---|---|
| `end_user_id` | Yes | Must match the session's actual owner, or this returns `404`. |

**Response `200`:**
```json
{
  "session_id": 42,
  "messages": [
    { "role": "user", "content": "What is Braxton Hicks?", "created_at": "2026-09-13T11:35:38Z" },
    { "role": "assistant", "content": "Braxton Hicks are...", "created_at": "2026-09-13T11:35:41Z" }
  ]
}
```

**Error responses:**
| Status | Condition |
|---|---|
| `401` | `X-API-Key` missing or invalid |
| `404` | Session doesn't exist, or doesn't belong to the given `end_user_id` under this product — both cases return the same `404`, deliberately, so a client can't distinguish "wrong ID" from "not yours." |
| `422` | `end_user_id` missing |
```

- [ ] **Step 2: Add both endpoints to `FRONTEND_INTEGRATION.md`**

In the "Features this API supports" list, add a new bullet after the
existing "Persistent chat history" bullet:

```markdown
- **Browse past conversations** — `GET /chat/sessions` lists a user's past
  conversations (most recent first, with a title from the first message);
  `GET /chat/sessions/{session_id}/messages` fetches one conversation's full
  history to render when the user reopens it. Use this to rebuild a
  conversation list after `localStorage` is cleared or on a new device.
```

In the "What it does *not* support" list, remove the line:
```markdown
- **No conversation list / history browsing endpoint** — the API only
  continues *one* conversation via `session_id`. If your product wants
  multiple named chat threads, that's a frontend-only concept (you manage
  multiple `session_id`s yourself); there's no server endpoint to list or
  fetch past sessions.
```
(This limitation no longer applies — the whole point of this feature is that
it's now supported.)

At the end of the "Reference implementation" code block, after the existing
`startNewConversation()` function, add:

```js
async function listConversations() {
  const res = await fetch(
    `${CHAT_API_URL.replace("/chat", "/chat/sessions")}?end_user_id=${encodeURIComponent(getEndUserId())}`,
    { headers: { "X-API-Key": CHAT_API_KEY } },
  );
  if (!res.ok) throw new Error(`Failed to list conversations (${res.status})`);
  const data = await res.json();
  return data.sessions; // [{ session_id, title, last_message_at, created_at }, ...]
}

async function openConversation(sessionId) {
  const res = await fetch(
    `${CHAT_API_URL.replace("/chat", "/chat/sessions")}/${sessionId}/messages?end_user_id=${encodeURIComponent(getEndUserId())}`,
    { headers: { "X-API-Key": CHAT_API_KEY } },
  );
  if (res.status === 404) throw new Error("Conversation not found");
  if (!res.ok) throw new Error(`Failed to load conversation (${res.status})`);
  const data = await res.json();
  localStorage.setItem("mamaroo_session_id", String(data.session_id));
  return data.messages; // [{ role, content, created_at }, ...] — render these, then continue with sendMessage()
}
```

- [ ] **Step 3: Commit**

```bash
git add API_CONTRACT.md FRONTEND_INTEGRATION.md
git commit -m "Document GET /chat/sessions and GET /chat/sessions/{id}/messages"
```

---

### Task 5: Deploy and verify live

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
`status` is `SUCCESS` — do not test against the live URL before this, since
the previous deployment keeps serving traffic during the build/health-check
window (confirmed earlier this session — see the Decisions log entry on
Railway rolling deploys).

- [ ] **Step 3: Run the smoke test against the live URL**

```bash
source .venv/bin/activate
python tests/smoke_test.py https://app-production-cc74.up.railway.app
```

Expected: `All checks passed.` If the LLM-dependent checks fail with a
`RESOURCE_EXHAUSTED`/`429` from whichever provider is currently configured
for `ANSWER_PROVIDER`, that's a quota issue, not a code issue (see the
Decisions log for the established handling of this) — re-run later or
temporarily hotswitch providers per `.env.example`, don't debug it as a bug
in this feature.

- [ ] **Step 4: Report the result**

State plainly whether the full smoke test passed against the live URL,
including the new thread-browsing checks, and paste the final
`N failure(s).` / `All checks passed.` line as evidence — not just "it looks
deployed."
