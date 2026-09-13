# Conversation Thread Browsing — Design

**Status:** Approved (design confirmed in chat 2026-09-13). Not yet implemented.
**Author:** Claude (session with the project owner), 2026-09-13.

## Problem

Today, "continuing a conversation" works (send a prior `session_id` back to
`/chat` and it resumes with full history), but there is no way for a client
to *discover* what `session_id`s exist for a given end user. If
`localStorage` is cleared, or a user opens the product on a new device, every
past conversation becomes permanently unreachable even though it still exists
in `chat_sessions`/`chat_messages`. This spec adds the two read-only endpoints
needed to browse and re-open past conversations. It does **not** change how
`/chat` itself works.

## Non-goals (explicitly out of scope for this spec)

- Deleting or renaming a thread.
- Any change to `POST /chat`'s request/response shape.
- Streaming (separate spec, not started).
- Rich media (not yet scoped — see the project's Decisions log).
- Cursor-based pagination — `limit`/`offset` is sufficient at this scale; can
  be revisited if a product accumulates enough threads per user for it to
  matter.
- Cross-product thread listing — a list is always scoped to the calling
  product's own `X-API-Key`, same isolation `/chat` already has.

## Data model

**No schema changes.** `chat_sessions` (`id`, `product_id`, `end_user_id`,
`created_at`) and `chat_messages` (`id`, `session_id`, `role`, `content`,
`created_at`) already carry everything both endpoints need.

## API design

### `GET /chat/sessions`

Lists a user's past conversations, most recently active first.

**Headers:** `X-API-Key` (required — reuses the same `auth.get_product`
FastAPI dependency `/chat` already uses, so it automatically inherits
identical API-key validation and origin-allow-list enforcement — no separate
auth logic to write).

**Query params:**
| Param | Type | Required | Notes |
|---|---|---|---|
| `end_user_id` | string | yes | Same identifier used in `POST /chat`. |
| `limit` | integer | no | Default 20, max 100. |
| `offset` | integer | no | Default 0. |

**Behavior:**
- Scoped to `(product_id from the API key, end_user_id)` — a product can
  only ever see its own end users' threads.
- **Excludes sessions with zero messages.** `app.py`'s `/chat` handler
  creates a `chat_sessions` row *before* calling `rag_engine.generate_answer`
  (see `app.py`'s `POST /chat` handler); if that call raises (a `500`, e.g.
  an LLM provider error), the session row persists with no messages ever
  written. These are failed-request artifacts, not real conversations, and
  must not appear in the list.
- Ordered by `last_message_at` descending (most recently active conversation
  first).

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
- `title` — the session's first **user** message, truncated to 80 characters
  with a trailing `…` if truncated. Never empty (a session only appears here
  if it has at least one message, and the first message in any session is
  always a `user` message — `app.py` only ever inserts `user` before
  `assistant` for a given turn).
- Empty `sessions: []` if the user has no conversations — not an error.

**Query (illustrative — implementation may phrase this differently as long
as behavior matches):**
```sql
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
```
Truncate `title_raw` to 80 chars in Python before returning (simpler and more
portable than SQL-side truncation, and the truncation rule — "…" suffix — is
presentation logic, not data).

**Errors:**
| Status | Condition |
|---|---|
| `401` | `X-API-Key` missing or invalid (same as `/chat`) |
| `422` | `end_user_id` missing (FastAPI query-param validation) |

### `GET /chat/sessions/{session_id}/messages`

Fetches one thread's full message history, in order.

**Headers:** `X-API-Key` (required — same `auth.get_product` dependency as
above).

**Query params:**
| Param | Type | Required | Notes |
|---|---|---|---|
| `end_user_id` | string | yes | Must match the session's actual owner. |

**Behavior — ownership check is mandatory, not optional:**
`session_id`s are sequential integers. Without verifying that the requested
session actually belongs to `(product_id, end_user_id)`, one end user could
read another end user's private conversation just by incrementing an integer.
The handler must:
1. Look up the session scoped to `product_id` (from the API key) AND
   `end_user_id` (from the query param) AND `id = session_id`.
2. If no row matches — **whether because the session doesn't exist at all,
   or because it exists but belongs to a different end user** — return `404`
   in both cases. Do not distinguish "doesn't exist" from "not yours" (that
   distinction would let an attacker enumerate valid session IDs even
   without being able to read them).

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
Messages ordered chronologically (`ORDER BY id`, matching how `/chat` already
loads history).

**Errors:**
| Status | Condition |
|---|---|
| `401` | `X-API-Key` missing or invalid |
| `404` | Session doesn't exist, or doesn't belong to the given `end_user_id` under this product |
| `422` | `end_user_id` missing |

## Response models (Pydantic, in `app.py`)

```python
class SessionSummary(BaseModel):
    session_id: int
    title: str
    last_message_at: datetime
    created_at: datetime

class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]

class MessageItem(BaseModel):
    role: str
    content: str
    created_at: datetime

class SessionMessagesResponse(BaseModel):
    session_id: int
    messages: list[MessageItem]
```

## Files touched

| File | Change |
|---|---|
| `app.py` | Two new route handlers + four new Pydantic models. No changes to the existing `/chat`, `/admin/products`, or `/health` handlers. |
| `db.py` | No changes — existing `fetchall`/`fetchone` helpers are sufficient. |
| `tests/smoke_test.py` | New checks: list threads, verify order/title, fetch one thread's messages, verify cross-user isolation (404 on a guessed/mismatched `session_id`). |
| `API_CONTRACT.md` | Document both new endpoints (same file/style as the existing three). |
| `FRONTEND_INTEGRATION.md` | Add both endpoints to the feature list and reference client, since the external frontend developer needs this to build thread browsing UI. |

## Testing plan

1. **Local, against the real dev Postgres** (same pattern as every other
   verification this session — no mocking of the database):
   - Create 2+ threads for one `end_user_id` via real `/chat` calls.
   - `GET /chat/sessions` → verify count, order (most recent first), titles
     match each thread's actual first message.
   - `GET /chat/sessions/{id}/messages` on one of them → verify full,
     correctly-ordered message content.
   - Cross-user isolation: same `session_id`, a *different* `end_user_id` →
     expect `404`.
   - Nonexistent `session_id` → expect `404` (same status as the isolation
     case, by design).
   - A session with zero messages (simulate by inserting a bare
     `chat_sessions` row with no `chat_messages`, mirroring the real failed-
     request scenario) → confirm it's excluded from `GET /chat/sessions`.
2. **Extend `tests/smoke_test.py`** with the above as automated checks,
   consistent with the existing self-cleaning pattern (create test data,
   assert, delete it).
3. **Live verification against the deployed Railway instance** after
   deploying, not just local — matching this project's established practice
   of never trusting a deploy based on `/health` alone.
