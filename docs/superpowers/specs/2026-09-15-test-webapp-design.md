# Password-Protected Test Webapp — Design

**Status:** Approved (design confirmed in chat 2026-09-15). Not yet implemented.
**Author:** Claude (session with the project owner), 2026-09-15.

## Problem

There's no way to demo or manually exercise the live chat service without
either handing someone a raw product API key (`tools/chat-tester.html`
requires typing one in) or writing a `curl` command. The project owner wants
a small, password-protected webapp — deployed as part of this same Railway
project — that anyone with the shared credential can open and chat through,
without ever seeing or being able to extract the real product API key.

## Non-goals (explicitly out of scope)

- **Multiple users / a user database.** One shared username/password pair,
  set via environment variables. Not a general auth system.
- **Streaming responses.** Plain request/response only, matching
  `tools/chat-tester.html`'s existing non-streaming behavior. Streaming can
  be added later if this tool's purpose expands.
- **Per-client (per-IP) lockout tracking.** Lockout is global and shared —
  5 failed attempts from anyone locks the login for everyone for 5 minutes.
  Simpler, and acceptable for a small internal test tool.
- **Persistent lockout/session state.** Lives in-process, in memory. A
  redeploy or restart clears it (nobody stays "logged out" or "locked out"
  across a deploy). This project already has Redis available in the same
  Railway project if this ever needs to change, but it's explicitly not
  used here — no reason to add a dependency for state that's fine to lose.
- **Thread/conversation browsing, multi-turn history UI polish, or any
  feature beyond "type a question, see an answer."** This is a test tool,
  not a product surface.
- **Changing anything in the main `MamaRoo-ChatService` codebase** (`app.py`,
  `cache.py`, etc.) — this is purely a new, separate service that calls the
  existing `/chat` endpoint exactly as any other client would.

## Decision: server-side proxy, not a client-side key

The chat service's own design treats its product API key as public-by-design
(safe in browser JS, like a Stripe publishable key — see
`MamaRoo-ChatService-Architecture.md`'s "Auth model" section). This new
webapp deliberately does **not** follow that pattern: the project owner
wants this specific tool to never expose the key at all, likely because it's
an internal demo surface rather than a real product integration.

Chosen approach: the webapp's own backend holds the real product API key in
a server-side-only environment variable and proxies every chat request
through itself. The browser only ever talks to the webapp's own backend
(same origin), never directly to `app-production-cc74.up.railway.app`. The
key never appears in browser DevTools' Network tab, page source, or
JavaScript — because the browser never has it.

## Decision: HTTP Basic Auth, not a custom login page

Two approaches were considered:

1. **HTTP Basic Auth** (chosen) — the backend returns `401` with a
   `WWW-Authenticate: Basic` header on any unauthenticated request; the
   browser shows its own native username/password prompt and caches the
   credentials for the browsing session. No login page HTML, no session
   cookie, no logout endpoint to build.
2. **Custom login form + signed session cookie** — a real login page and a
   logout button; more code (token generation/validation, cookie handling,
   a login page) for a nicer but unnecessary UX given this tool's purpose.

Chosen: **(1)**, confirmed directly with the project owner as matching the
"keep simple" ask. It satisfies every stated requirement (username/password
gate, 5-attempts-then-lockout) with the least code.

## Auth & lockout mechanics

**Credentials:** `WEBAPP_USERNAME` / `WEBAPP_PASSWORD`, read from
environment variables at startup, compared using `secrets.compare_digest`
(constant-time comparison — avoids leaking match-length via timing).

**Lockout state:** one small module-level object (mirroring this project's
existing `db.py`/`redis_client.py` eager-singleton convention), holding:
- `failed_attempts: int` — consecutive failures since the last success or
  the last lockout.
- `locked_until: float | None` — a `time.time()`-style timestamp; `None`
  or in the past means not locked.

**On every request** (a FastAPI dependency applied to every route):
1. If `locked_until` is set and in the future: return `429 Too Many
   Requests` immediately, without even checking the submitted credentials.
   Body: a short message stating when it unlocks (e.g. "Too many failed
   attempts. Try again in N seconds.").
2. Otherwise, parse the `Authorization: Basic` header. Missing or malformed
   → treat as a failed attempt (see step 3).
3. Compare the decoded username/password against `WEBAPP_USERNAME`/
   `WEBAPP_PASSWORD` via `secrets.compare_digest`.
   - **Match:** reset `failed_attempts` to 0, allow the request through.
   - **No match:** increment `failed_attempts`. If it now equals 5, set
     `locked_until = time.time() + 300` and reset `failed_attempts` to 0
     (so the next window starts clean after the lockout expires). Return
     `401` with `WWW-Authenticate: Basic realm="..."` so the browser
     re-prompts.

This means: 5 wrong attempts in a row → locked for 5 minutes. A correct
login at any point before the 5th failure resets the counter (only
*consecutive* failures count, matching the plain reading of "after 5 failed
attempts").

## Chat proxy

`POST /chat` on the webapp (behind the same auth dependency as every other
route) accepts the same request shape the real `/chat` expects
(`{message, end_user_id, session_id}`), and does the following:
1. Runs the auth/lockout check (step above).
2. Forwards the request to `{CHATSERVICE_URL}/chat` using `httpx` (already
   a dependency elsewhere in this project — `tests/smoke_test.py`), adding
   `X-API-Key: {CHATSERVICE_API_KEY}` server-side. `end_user_id` is
   generated client-side once (a random ID stored in `localStorage`, same
   pattern as `tools/chat-tester.html`) and passed through unchanged —
   the webapp doesn't need to know or care about identity beyond that.
3. Returns the real service's JSON response body and status code
   unmodified (so error shapes — `400`/`401`/`403`/`429`/`500` from the
   *real* service — pass through as-is; the webapp's own `401`/`429` are
   only for its own login gate, at a different layer).

No caching, no rate-limit logic, no session persistence of its own — the
real chat service already does all of that. This proxy is intentionally
thin.

## Frontend

A single static HTML page (`webapp/static/index.html`), adapted from the
existing `tools/chat-tester.html`: same message-log UI, input box, send
button, source citations, and cache-hit badge — with the API key input
field removed entirely (there's nothing for the user to type or see; the
backend injects the real key). The base URL is also removed — it always
calls same-origin `/chat`.

## File map (new)

```
webapp/
├── app.py              FastAPI: auth dependency, GET / (serves index.html),
│                        POST /chat (proxy)
├── config.py            Reads WEBAPP_USERNAME, WEBAPP_PASSWORD,
│                        CHATSERVICE_URL, CHATSERVICE_API_KEY from env
├── auth.py               Basic-auth check + lockout state + dependency
├── static/
│   └── index.html        Adapted chat-tester.html, no API key field
├── requirements.txt      fastapi, uvicorn, httpx — nothing else
├── Dockerfile            Minimal Python image, mirrors the main service's
│                          Dockerfile shape
└── .dockerignore
```

Deliberately separate from the main service's `requirements.txt` /
`Dockerfile` — this app doesn't need Postgres, Redis, `sentence-transformers`,
or any LLM SDK, so its own dependency list stays tiny and its Docker image
stays small and fast to build (unlike the main service's multi-minute
`torch`/`sentence-transformers` build).

## Deployment

New Railway service (`webapp`) in the existing `MamaRoo-ChatService`
project (id `b5d5a575-6ea3-4e92-980d-9905f84fe872`), connected to this same
GitHub repo with `rootDirectory` set to `webapp/` so it builds and deploys
independently of the main `app` service. Variables set directly on this new
service:
- `WEBAPP_USERNAME` / `WEBAPP_PASSWORD` — newly generated credentials
  (shared with the project owner out of band, not committed anywhere).
- `CHATSERVICE_URL` — `http://${{app.RAILWAY_PRIVATE_DOMAIN}}` (Railway's
  private networking between services in the same project/environment,
  already used for `${{Postgres.DATABASE_URL}}` elsewhere in this project —
  internal traffic stays off the public internet entirely, no reason to
  round-trip through the public domain for a server-to-server call within
  the same project).
- `CHATSERVICE_API_KEY` — a real product API key. A **new**, dedicated
  product will be provisioned via the existing `POST /admin/products`
  endpoint specifically for this webapp (not reusing an existing test
  product), so its usage/rate-limit is isolated and easy to identify/revoke
  independently later.
- A public domain generated for this new service (`generate-domain`), so
  the project owner can share a URL.

## Testing

No automated test suite for this tool — it's a small internal utility, not
a product feature, and this project's own `tests/smoke_test.py` convention
is reserved for the main chat service. Verification is manual, by hand,
against the real deployed webapp:
- Correct credentials → prompt succeeds, chat UI loads, a real question
  gets a real grounded answer via the proxy (confirms the API key was
  injected correctly server-side and never appeared in the browser).
- Wrong credentials × 5 in a row → the 5th attempt (or the request right
  after it) returns `429`; a 6th attempt with *correct* credentials during
  the lockout window is still rejected with `429`; after the 5-minute
  window, correct credentials succeed again.
- A correct login after 1-4 failures resets the counter (verified by then
  failing 4 more times without triggering a lockout on the 5th of *those*,
  i.e. confirming only consecutive failures count).
- Browser DevTools Network tab inspected during a real chat exchange to
  confirm no request ever carries `CHATSERVICE_API_KEY`'s value.
