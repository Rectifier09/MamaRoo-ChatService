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
