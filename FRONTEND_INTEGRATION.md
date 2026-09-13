# MamaRoo Chat — Frontend Integration Guide

> Drop this file into your Claude Code session and ask it to build the chat
> integration against this spec. Everything you need — endpoint, auth, request/
> response shapes, error handling, and UX expectations — is below. No access to
> the backend repo is required to build against this.

## What this is

A hosted chat API that answers pregnancy-related questions from a curated
knowledge base (currently the Mayo Clinic pregnancy library — trimester
overviews, prenatal care, labor/delivery, postpartum, nutrition, vaccines,
etc.). It's a **backend you call directly from the browser** — there is no
frontend of its own. Your job is to build the chat UI and wire it to the one
endpoint below.

Under the hood it does retrieval-augmented generation (rewrites follow-up
questions using conversation history, retrieves relevant knowledge-base
excerpts, and generates an answer grounded only in those excerpts — it will
say it doesn't know rather than guess). None of that matters for integration;
it's mentioned so you understand *why* answers are cited and *why* the same
question can occasionally take a few seconds.

## Quick facts

| | |
|---|---|
| **Base URL** | `https://app-production-cc74.up.railway.app` |
| **The one endpoint you need** | `POST /chat` |
| **Auth header** | `X-API-Key` (see below — safe to hardcode in client code) |
| **Content type** | `application/json` |
| **Response time** | Typically 1–5s; occasionally longer or a one-off failure under upstream load (see Error handling) |
| **Streaming** | Not supported — you get the full answer in one response |

## Your API key

```
pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF
```

This is a **public key by design** — the same category as a Stripe
*publishable* key or a Google Maps *browser* key, not a secret. It's meant to
be embedded directly in client-side JS, committed to your repo, visible in the
network tab — all fine. It identifies your product to the backend (so usage
and rate limits are tracked separately from other integrations) and is
currently allowed from **any origin** (`allowed_origins: ["*"]`), so it works
from `localhost` during development with zero config.

**Before shipping to production**, ping the project owner to lock this key
(or a new one) to your real domain(s) — that's a one-line change on their end,
not yours.

## Features this API supports

- **Grounded Q&A** — answers come only from the knowledge base; if the
  knowledge base doesn't cover something, it says so rather than making
  something up.
- **Source citations** — every response includes which knowledge-base
  file(s) the answer drew from (`sources` array). Useful for a "based on..."
  footnote in your UI.
- **Multi-turn conversations** — send back the `session_id` from a prior
  response and the backend resolves follow-ups against history server-side
  (e.g. user asks "What is Braxton Hicks?", then "when do they usually
  happen?" — the second question is understood in context). **You never need
  to resend prior messages** — just the new one plus the `session_id`.
- **Persistent chat history** — conversations are stored server-side per
  `(your API key, end_user_id)`. A returning user with the same
  `end_user_id` retains their session if you keep passing the same
  `session_id`.
- **Browse past conversations** — `GET /chat/sessions` lists a user's past
  conversations (most recent first, with a title from the first message);
  `GET /chat/sessions/{session_id}/messages` fetches one conversation's full
  history to render when the user reopens it. Use this to rebuild a
  conversation list after `localStorage` is cleared or on a new device.
- **Response caching (transparent to you)** — semantically similar repeat
  questions may come back near-instantly (`cached: true` in the response).
  No special handling needed on your end, but it explains why some responses
  are much faster than others.
- **Per-product rate limiting** — your key has a request-per-minute cap (ask
  the project owner what yours is set to). Exceeding it returns `429`.

## What it does *not* support (design around these)

- **No streaming** — don't build a token-by-token typing effect; show a
  loading state until the full response arrives.
- **No message editing/deletion/regeneration** — every user message is
  final and appended to history server-side.
- **No images/attachments** — text in, text out.
- **No typing indicators or partial results from the server** — build your
  own loading UI.

## The endpoint

### `POST /chat`

**Headers**
```
Content-Type: application/json
X-API-Key: pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF
```

**Request body**
```json
{
  "message": "What is the refund window for annual plans?",
  "end_user_id": "a stable id for this end user",
  "session_id": null
}
```
| Field | Type | Required | Notes |
|---|---|---|---|
| `message` | string | yes | The user's message. Must not be empty/whitespace-only. |
| `end_user_id` | string | yes | A stable identifier for this browser/user — a real logged-in user ID if you have auth, otherwise a client-generated UUID persisted in `localStorage`. Never sent to any LLM; used only to scope conversation history. |
| `session_id` | integer or `null` | yes (send `null` if starting fresh) | Omit/`null` to start a new conversation. Send back the `session_id` from a prior response to continue it. |

**Success response — `200`**
```json
{
  "answer": "Annual plans can be refunded within 30 days of purchase.",
  "sources": ["pricing-faq.md"],
  "session_id": 42,
  "cached": false
}
```
| Field | Notes |
|---|---|
| `answer` | Plain text/markdown-ish string. Render as text — no HTML is embedded, but some models format with `**bold**`/lists, so a lightweight markdown renderer looks nicer than raw text. |
| `sources` | Array of source filenames (may be empty if nothing relevant was found — not an error). Filenames are internal (e.g. `Mayo-Clinic-Pregnancy-Library.md`) — consider mapping to a friendlier label rather than showing raw filenames to end users. |
| `session_id` | **Store this** and send it back on the next message in the same conversation. |
| `cached` | `true` if this answer came from cache (no LLM call). Optional to surface in UI (e.g. a subtle "instant answer" badge) — safe to ignore. |

**Error responses**
| Status | Meaning | What to show the user |
|---|---|---|
| `400` | `message` was empty/whitespace | Validate client-side too so this rarely happens; if it does, a plain "please enter a message." |
| `401` | API key missing or invalid | A config problem on your end, not the user's — shouldn't happen in production. Log it, don't show raw error text to users. |
| `403` | Key valid but your page's origin isn't allow-listed for it | Same as above — a config problem to fix with the project owner, not a user-facing state. |
| `429` | Rate limit exceeded | Show something friendly like "Too many questions at once — try again in a moment," not the raw error. |
| `500` | Unhandled error (DB issue, LLM provider hiccup, etc.) | **Retry once automatically** after a short delay (1–2s) — a fraction of these are transient upstream LLM overload, not real failures. If it fails twice, show a generic "something went wrong, please try again." |

### `GET /health`
No auth, no body. Returns `{"status": "ok"}`. Useful for an uptime check, not
useful for confirming the chat feature itself works (it deliberately doesn't
touch the database or the LLM).

## Reference implementation (copy this pattern)

```js
const CHAT_API_URL = "https://app-production-cc74.up.railway.app/chat";
const CHAT_API_KEY = "pk_LuWWNp6ExBUBcwMSMrJAHBrn5mFP8jTF";

function getEndUserId() {
  let id = localStorage.getItem("mamaroo_end_user_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("mamaroo_end_user_id", id);
  }
  return id;
}

async function sendMessage(text, { retry = true } = {}) {
  const sessionId = localStorage.getItem("mamaroo_session_id");
  const res = await fetch(CHAT_API_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": CHAT_API_KEY },
    body: JSON.stringify({
      message: text,
      end_user_id: getEndUserId(),
      session_id: sessionId ? Number(sessionId) : null,
    }),
  });

  if (res.status >= 500 && retry) {
    await new Promise((r) => setTimeout(r, 1500));
    return sendMessage(text, { retry: false }); // one retry only
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(`Chat request failed (${res.status}): ${body.detail ?? "unknown error"}`);
  }

  const data = await res.json();
  localStorage.setItem("mamaroo_session_id", String(data.session_id));
  return data; // { answer, sources, session_id, cached }
}

function startNewConversation() {
  localStorage.removeItem("mamaroo_session_id");
}

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

A working, already-tested example of this exact flow (with a full chat UI,
loading state, error display, and a "New chat" button) lives at
`tools/chat-tester.html` in the backend repo if you have access to it — open
it directly in a browser, no build step, to see the whole thing running live.

## UX recommendations

- **Loading state**: show a typing/thinking indicator while waiting — most
  responses land in 1–5s, but don't assume instant.
- **Empty-state / first message**: no need to pre-seed a greeting from the
  API — it only responds to real questions. A static "Ask me anything about
  pregnancy" placeholder in your UI is fine.
- **"New conversation" action**: clear the stored `session_id` (see
  `startNewConversation()` above) — don't clear `end_user_id`, that identifies
  the *user*, not the conversation.
- **Citations**: showing `sources` builds trust (this is health information),
  but raw filenames aren't user-friendly — consider a small "Source: Mayo
  Clinic pregnancy guide" line instead of the literal filename.
- **Rate-limit / error copy**: never surface raw `detail` strings from `4xx`/
  `5xx` responses to end users — map them to friendly copy per the table
  above.

## Questions to ask the project owner before shipping

1. What real production domain(s) should be allow-listed for your API key
   (currently open to any origin — fine for dev, should be locked down before
   launch)?
2. What's your product's actual `rate_limit_per_minute`? (Ask if the current
   key's limit is enough for expected traffic.)
3. Do you have real user IDs to pass as `end_user_id`, or should it stay an
   anonymous per-browser UUID?
