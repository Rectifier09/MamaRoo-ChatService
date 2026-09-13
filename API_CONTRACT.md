# API Contract

Base URL: local `http://localhost:8000`, or the Railway-generated domain once deployed.

## `GET /health`

No auth. Liveness check for Railway's health checker (`railway.toml` points at this).

**Response `200`:**
```json
{ "status": "ok" }
```

## `POST /chat`

The endpoint end-user-facing frontends call.

**Headers:**
| Header | Required | Notes |
|---|---|---|
| `X-API-Key` | Yes | Issued via `POST /admin/products`. Public value — see `ARCHITECTURE.md` → "Auth model". |
| `Origin` | Sent automatically by browsers | Checked against the product's `allowed_origins` unless it contains `"*"`. |
| `Content-Type` | Yes | `application/json` |

**Request body:**
```json
{
  "message": "What's the refund window for annual plans?",
  "end_user_id": "a stable id for this end user",
  "session_id": null
}
```
- `message` (string, required) — must not be empty/whitespace-only → `400` otherwise.
- `end_user_id` (string, required) — a real logged-in user id if the product has one,
  otherwise a client-generated UUID persisted in `localStorage`. Used only to scope
  `chat_sessions`, never sent to the LLM.
- `session_id` (integer, optional) — omit or send `null` to start a new conversation.
  Send back the `session_id` from a prior response to continue that conversation
  (the server loads recent history itself — the client does not resend prior turns).
- `stream` (boolean, optional, default `false`) — when `true`, the response
  is `text/event-stream` instead of a single JSON body (see below). When
  absent or `false`, behavior is unchanged from the rest of this section.

**Response `200`:**
```json
{
  "answer": "Annual plans can be refunded within 30 days of purchase.",
  "sources": ["pricing-faq.md"],
  "session_id": 42,
  "cached": false
}
```
- `sources` — deduplicated, sorted list of source filenames the answer drew on. Empty
  array if nothing was retrieved (e.g., an empty knowledge base) — this is not an
  error condition.
- `cached` — `true` if a semantic cache hit answered this without an LLM call.

**Error responses:**
| Status | Condition |
|---|---|
| `400` | `message` is empty/whitespace-only |
| `401` | `X-API-Key` missing, or doesn't match any product |
| `403` | Valid key, but request `Origin` isn't in that product's `allowed_origins` |
| `429` | Product has exceeded `rate_limit_per_minute` |
| `500` | Unhandled error (DB down, Anthropic API error, etc.) — body is `{"detail": "<message>"}`; don't let internals leak beyond a plain message |

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

## `POST /admin/products`

Provisions a new product (tenant) and its API key. Not called by end-user frontends —
this is an operator/admin action (you, running a `curl` command or a small internal
tool), not part of the public product surface.

**Headers:**
| Header | Required |
|---|---|
| `X-Admin-Key` | Yes — must equal `config.ADMIN_API_KEY` |
| `Content-Type` | `application/json` |

**Request body:**
```json
{
  "name": "MamaRoo",
  "allowed_origins": ["https://mamaroo.app", "http://localhost:5173"],
  "rate_limit_per_minute": 30
}
```
- `name` (string, required)
- `allowed_origins` (array of strings, optional, default `[]`) — use `["*"]` to allow
  any origin (fine for local dev, not recommended once a product is live).
- `rate_limit_per_minute` (integer, optional) — defaults to
  `config.DEFAULT_RATE_LIMIT_PER_MINUTE` if omitted.

**Response `200`:**
```json
{ "id": 1, "api_key": "pk_9f2a1c...restofkey" }
```

The key is generated server-side (`secrets.token_urlsafe`) and returned exactly once
in this response. There's no "list keys" or "reveal key" endpoint in Phase 1 — if it's
lost, provision a new product entry rather than trying to recover the old key.

**Error responses:**
| Status | Condition |
|---|---|
| `401` | `X-Admin-Key` missing or incorrect |

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

## Example: minimal browser integration

```js
const CHAT_API_KEY = "pk_..."; // safe to ship in client code — see ARCHITECTURE.md
const endUserId = localStorage.getItem("endUserId") ?? crypto.randomUUID();
localStorage.setItem("endUserId", endUserId);

async function sendMessage(text) {
  const sessionId = localStorage.getItem("chatSessionId"); // null on first message
  const res = await fetch("https://<your-railway-domain>/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": CHAT_API_KEY },
    body: JSON.stringify({
      message: text,
      end_user_id: endUserId,
      session_id: sessionId ? Number(sessionId) : null,
    }),
  });
  if (!res.ok) throw new Error(`Chat request failed: ${res.status}`);
  const data = await res.json();
  localStorage.setItem("chatSessionId", String(data.session_id));
  return data; // { answer, sources, session_id, cached }
}
```
