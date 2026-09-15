# Password-Protected Test Webapp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a small, password-protected webapp (`webapp/`) that
lets anyone with a shared login chat with the live MamaRoo-ChatService
through a browser, without ever exposing the real product API key.

**Architecture:** A separate, minimal FastAPI service with its own
`requirements.txt`/`Dockerfile`, deployed as a second Railway service in the
same project. HTTP Basic Auth gates every route (a global in-memory
5-failed-attempts / 5-minute lockout); a thin server-side proxy forwards
`POST /chat` to the real chat service with the real API key injected only
on the server side. The frontend is a single static HTML page adapted from
`tools/chat-tester.html`, with the API key field removed entirely.

**Tech Stack:** FastAPI, `uvicorn`, `httpx`, `python-dotenv` — no database,
no Redis, no LLM SDK. Deployed on Railway (Dockerfile builder, matching the
main service's deploy pattern).

**Spec:** `docs/superpowers/specs/2026-09-15-test-webapp-design.md`

## Global Constraints

- The browser must never receive `CHATSERVICE_API_KEY`'s value in any
  response body, header, or page source — it lives only in the webapp
  backend's environment, injected server-side into the proxied request.
- `WEBAPP_USERNAME`/`WEBAPP_PASSWORD` are compared with
  `secrets.compare_digest`, never `==` or `in`.
- Lockout is global (not per-IP): 5 consecutive failed credential
  submissions → every route returns `429` for the next 300 seconds,
  regardless of who submits. A successful login before the 5th failure
  resets the counter to 0. A completely missing `Authorization` header
  (the browser's very first, credential-less request) does **not** count
  as a failed attempt — only a header that was actually sent with wrong
  values does.
- `webapp/` is fully separate from the main `MamaRoo-ChatService` codebase
  at the repo root — its own `requirements.txt`, `Dockerfile`, `config.py`.
  Nothing in this plan modifies any file outside `webapp/`.
- `GET /health` on the webapp is unauthenticated (dependency-free),
  matching the main service's own healthcheck convention — Railway's
  healthcheck must succeed independent of login credentials.

---

## Prerequisite (controller does this, not a subagent — needs a live admin action against production and must handle the real API key/credentials directly, not via a subagent dispatch)

Before Task 1 is dispatched, the controller must:

1. Provision a new product on the live chat service for this webapp
   specifically (not reusing an existing test product), via:
   ```bash
   curl -s -X POST https://app-production-cc74.up.railway.app/admin/products \
     -H "X-Admin-Key: <the real ADMIN_API_KEY, from this repo's root .env>" \
     -H "Content-Type: application/json" \
     -d '{"name": "webapp-tester", "allowed_origins": ["*"], "rate_limit_per_minute": 30}'
   ```
   `allowed_origins: ["*"]` is correct here (not a security gap): this key
   is only ever used by the webapp's own server-side `httpx` call, which
   sends no browser `Origin` header at all — origin restriction has no
   meaning for a server-to-server caller. Record the returned `api_key`
   (`pk_...`) — this is `CHATSERVICE_API_KEY` for every step below.
2. Generate real credentials for the login gate — not placeholders:
   ```bash
   python3 -c "import secrets; print('WEBAPP_USERNAME=' + secrets.token_hex(4)); print('WEBAPP_PASSWORD=' + secrets.token_urlsafe(18))"
   ```
   This is a public-facing login on the internet once deployed — use the
   real generated values, not `admin`/`admin`.
3. Create `webapp/.env` (git-ignored — the repo's root `.gitignore` already
   has a bare `.env` entry, which matches at any depth) with the four
   values:
   ```
   WEBAPP_USERNAME=<from step 2>
   WEBAPP_PASSWORD=<from step 2>
   CHATSERVICE_URL=https://app-production-cc74.up.railway.app
   CHATSERVICE_API_KEY=<from step 1>
   ```
   (`CHATSERVICE_URL` is the public URL for local dev — the private
   `RAILWAY_PRIVATE_DOMAIN` reference only resolves inside Railway's own
   network, and is set separately on the deployed service in Task 4.)
4. Confirm the key works with one real call before moving on:
   ```bash
   curl -s -X POST https://app-production-cc74.up.railway.app/chat \
     -H "X-API-Key: <the pk_... key from step 1>" -H "Content-Type: application/json" \
     -d '{"message":"What is Braxton Hicks?","end_user_id":"webapp-prereq-check","session_id":null}'
   ```
   Expected: `200`, a real grounded answer. Clean up this test's session
   afterward, from the main repo root (not `webapp/`) using its own
   `.venv`:
   ```bash
   source .venv/bin/activate
   python3 -c "
   import db
   db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = %s)\", ('webapp-prereq-check',))
   db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = %s\", ('webapp-prereq-check',))
   "
   deactivate
   ```

Task 1's implementer assumes `webapp/.env` already exists with working
values by the time it's dispatched — same pattern as the Redis migration
plan's own Prerequisite.

---

## Task 1: Scaffold, config, and the auth/lockout module

**Files:**
- Create: `webapp/requirements.txt`
- Create: `webapp/.env.example`
- Create: `webapp/config.py`
- Create: `webapp/auth.py`

**Interfaces:**
- Produces: `config.WEBAPP_USERNAME: str`, `config.WEBAPP_PASSWORD: str`,
  `config.CHATSERVICE_URL: str`, `config.CHATSERVICE_API_KEY: str`.
- Produces: `auth.check_auth` — a FastAPI dependency function (no
  parameters beyond FastAPI's own injected `HTTPBasicCredentials | None`)
  that raises `HTTPException(401, ...)` or `HTTPException(429, ...)` on
  failure, returns `None` on success. Task 2 imports this directly:
  `from auth import check_auth`.

- [ ] **Step 1: Create `webapp/requirements.txt`**

```
fastapi
uvicorn[standard]
httpx
python-dotenv
```

- [ ] **Step 2: Create `webapp/.env.example`**

```
WEBAPP_USERNAME=changeme
WEBAPP_PASSWORD=changeme
CHATSERVICE_URL=https://app-production-cc74.up.railway.app
CHATSERVICE_API_KEY=pk_...
```

- [ ] **Step 3: Create `webapp/config.py`**

```python
"""
Central configuration for the test webapp. All values can be overridden via
environment variables (or a .env file -- see .env.example). See
docs/superpowers/specs/2026-09-15-test-webapp-design.md for what each is for.
"""
import os
from dotenv import load_dotenv

load_dotenv()

WEBAPP_USERNAME = os.environ.get("WEBAPP_USERNAME", "")
WEBAPP_PASSWORD = os.environ.get("WEBAPP_PASSWORD", "")
CHATSERVICE_URL = os.environ.get("CHATSERVICE_URL", "")
CHATSERVICE_API_KEY = os.environ.get("CHATSERVICE_API_KEY", "")
```

- [ ] **Step 4: Create `webapp/auth.py`**

```python
"""
HTTP Basic Auth gate with a global, in-memory lockout: 5 consecutive failed
credential submissions locks out ALL logins (not just the failing client)
for 5 minutes. This is a shared single-credential test tool, not a
multi-user system -- see
docs/superpowers/specs/2026-09-15-test-webapp-design.md.

State lives in a single module-level object, eager at import time --
mirrors the main MamaRoo-ChatService's db.py/redis_client.py convention.
Resets on process restart/redeploy; that's an accepted trade-off for this
internal test tool, not a bug.
"""
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config

# auto_error=False: a request with no Authorization header at all gets
# `None` here instead of FastAPI auto-raising 401 itself -- we need to run
# our own lockout check even when no credentials were submitted yet (the
# browser's very first, pre-prompt request).
security = HTTPBasic(auto_error=False)

LOCKOUT_THRESHOLD = 5
LOCKOUT_SECONDS = 300


class _LoginState:
    def __init__(self):
        self.failed_attempts = 0
        self.locked_until: Optional[float] = None


_state = _LoginState()


def check_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    if _state.locked_until is not None and time.time() < _state.locked_until:
        remaining = int(_state.locked_until - time.time())
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts. Try again in {remaining} seconds.",
        )
    _state.locked_until = None

    if credentials is None:
        # No credentials submitted yet -- prompt the browser. Doesn't count
        # as a failed attempt: this is the browser's first, credential-less
        # probe, not a user actually trying and failing.
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="MamaRoo Chat Test"'},
        )

    username_ok = secrets.compare_digest(credentials.username, config.WEBAPP_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, config.WEBAPP_PASSWORD)
    if username_ok and password_ok:
        _state.failed_attempts = 0
        return

    _state.failed_attempts += 1
    if _state.failed_attempts >= LOCKOUT_THRESHOLD:
        _state.locked_until = time.time() + LOCKOUT_SECONDS
        _state.failed_attempts = 0
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed login attempts. Try again in {LOCKOUT_SECONDS} seconds.",
        )

    raise HTTPException(
        status_code=401,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": 'Basic realm="MamaRoo Chat Test"'},
    )
```

- [ ] **Step 5: Set up a local venv and install dependencies**

```bash
cd webapp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

- [ ] **Step 6: Verify the lockout logic by hand**

From `webapp/`, with the venv active and `webapp/.env` already present
(created in the Prerequisite step):

```bash
python3 -c "
from fastapi.security import HTTPBasicCredentials
from auth import check_auth, _state, LOCKOUT_THRESHOLD
import config

good = HTTPBasicCredentials(username=config.WEBAPP_USERNAME, password=config.WEBAPP_PASSWORD)
bad = HTTPBasicCredentials(username='wrong', password='wrong')

# No credentials at all -- 401, not counted as a failure.
try:
    check_auth(None)
    print('FAIL: expected 401 for no credentials')
except Exception as e:
    print('no creds:', e.status_code, '-- failed_attempts:', _state.failed_attempts)

# 4 wrong attempts -- still 401, not yet locked.
for i in range(4):
    try:
        check_auth(bad)
        print('FAIL: expected 401 on wrong creds')
    except Exception as e:
        print(f'wrong attempt {i+1}:', e.status_code, '-- failed_attempts:', _state.failed_attempts)

# A correct login here should reset the counter to 0.
check_auth(good)
print('correct login -- failed_attempts reset to:', _state.failed_attempts)

# Now drive it to an actual lockout: 5 wrong in a row.
for i in range(LOCKOUT_THRESHOLD):
    try:
        check_auth(bad)
        print(f'FAIL: expected exception on attempt {i+1}')
    except Exception as e:
        print(f'lockout-drive attempt {i+1}:', e.status_code, '-- locked_until set:', _state.locked_until is not None)

# Locked out now -- even CORRECT credentials should still get 429.
try:
    check_auth(good)
    print('FAIL: expected 429 while locked out, even with correct creds')
except Exception as e:
    print('correct creds while locked:', e.status_code)

# Simulate the lockout window expiring.
_state.locked_until = 0.0
check_auth(good)
print('after lockout expires, correct creds succeed -- failed_attempts:', _state.failed_attempts)
"
```

Expected output (exact values may vary slightly in wording but must match
this shape):
```
no creds: 401 -- failed_attempts: 0
wrong attempt 1: 401 -- failed_attempts: 1
wrong attempt 2: 401 -- failed_attempts: 2
wrong attempt 3: 401 -- failed_attempts: 3
wrong attempt 4: 401 -- failed_attempts: 4
correct login -- failed_attempts reset to: 0
lockout-drive attempt 1: 401 -- locked_until set: False
lockout-drive attempt 2: 401 -- locked_until set: False
lockout-drive attempt 3: 401 -- locked_until set: False
lockout-drive attempt 4: 401 -- locked_until set: False
lockout-drive attempt 5: 429 -- locked_until set: True
correct creds while locked: 429
after lockout expires, correct creds succeed -- failed_attempts: 0
```
This is a real, direct test of `check_auth`'s logic (not a server, not
mocks) — it imports and calls the actual function with real
`HTTPBasicCredentials` objects. If any line doesn't match, the logic has a
bug — fix `auth.py`, don't adjust this test to match wrong behavior.

- [ ] **Step 7: Commit**

```bash
git add webapp/requirements.txt webapp/.env.example webapp/config.py webapp/auth.py
git commit -m "Add test webapp scaffold: config + HTTP Basic Auth with global lockout"
```

---

## Task 2: FastAPI app (proxy) and the frontend

**Files:**
- Create: `webapp/app.py`
- Create: `webapp/static/index.html`

**Interfaces:**
- Consumes: `auth.check_auth` (Task 1), `config.CHATSERVICE_URL`,
  `config.CHATSERVICE_API_KEY` (Task 1).
- Produces: a running FastAPI app with `GET /health` (unauthenticated),
  `GET /` (authenticated, serves `static/index.html`), `POST /chat`
  (authenticated, proxies to the real chat service).

- [ ] **Step 1: Create `webapp/app.py`**

```python
"""
Password-protected test webapp for MamaRoo-ChatService. A thin proxy: the
browser only ever talks to this service, never to the real chat service --
the real product API key lives only in this process's environment,
injected server-side into the proxied request. See
docs/superpowers/specs/2026-09-15-test-webapp-design.md.

Run with:
    uvicorn app:app --reload --port 8001
"""
from typing import Optional

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from auth import check_auth

app = FastAPI(title="MamaRoo-ChatService Test Webapp")


class ChatRequest(BaseModel):
    message: str
    end_user_id: str
    session_id: Optional[int] = None


@app.get("/health")
def health():
    # Deliberately dependency-free, matching the main service's own
    # pattern -- Railway's healthcheck must succeed independent of login
    # credentials or the real chat service's availability.
    return {"status": "ok"}


@app.get("/", dependencies=[Depends(check_auth)])
def index():
    return FileResponse("static/index.html")


@app.post("/chat", dependencies=[Depends(check_auth)])
async def chat(req: ChatRequest):
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{config.CHATSERVICE_URL}/chat",
            headers={"X-API-Key": config.CHATSERVICE_API_KEY},
            json=req.model_dump(),
        )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)
```

- [ ] **Step 2: Create `webapp/static/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>MamaRoo Chat Service — Test Webapp</title>
<style>
  :root { color-scheme: light; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 720px;
    margin: 0 auto;
    padding: 24px 16px 80px;
    background: #f7f5f2;
    color: #222;
  }
  h1 { font-size: 1.25rem; margin-bottom: 4px; }
  p.sub { color: #666; margin-top: 0; font-size: 0.9rem; }

  #log {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 16px;
  }
  .msg { max-width: 85%; padding: 10px 14px; border-radius: 14px; white-space: pre-wrap; line-height: 1.4; }
  .msg.user { align-self: flex-end; background: #dce7ff; border-bottom-right-radius: 4px; }
  .msg.assistant { align-self: flex-start; background: #fff; border: 1px solid #e2e2e2; border-bottom-left-radius: 4px; }
  .msg.error { align-self: flex-start; background: #ffe5e5; border: 1px solid #ffb3b3; color: #8a1f1f; }
  .meta { font-size: 0.72rem; color: #888; margin-top: 6px; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 6px; font-size: 0.68rem; margin-right: 6px; }
  .badge.cached { background: #d7f5dd; color: #1b6a2e; }
  .badge.miss { background: #eee; color: #666; }

  #composer { display: flex; gap: 8px; position: sticky; bottom: 12px; }
  #composer input[type=text] {
    flex: 1;
    padding: 12px 14px;
    border-radius: 10px;
    border: 1px solid #ccc;
    font-size: 0.95rem;
  }
  #composer button, #reset {
    padding: 10px 16px;
    border: none;
    border-radius: 10px;
    background: #2b2b2b;
    color: #fff;
    font-size: 0.9rem;
    cursor: pointer;
  }
  #reset { background: #888; margin-left: 8px; }
  #composer button:disabled { opacity: 0.5; cursor: default; }

  #status { font-size: 0.78rem; color: #888; min-height: 1.2em; margin-top: 8px; }
</style>
</head>
<body>
  <h1>MamaRoo Chat Service — Test Webapp</h1>
  <p class="sub">Ask a question from the knowledge base below.</p>

  <div id="log"></div>
  <div id="status"></div>

  <div id="composer">
    <input id="input" type="text" placeholder="Ask something from the knowledge base..." autofocus>
    <button id="send">Send</button>
    <button id="reset" title="Start a new conversation">New chat</button>
  </div>

<script>
const log = document.getElementById('log');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const resetBtn = document.getElementById('reset');
const statusEl = document.getElementById('status');

const endUserId = localStorage.getItem('mamaroo_end_user_id') || crypto.randomUUID();
localStorage.setItem('mamaroo_end_user_id', endUserId);
const sessionKey = 'mamaroo_webapp_session_id';

function addMessage(role, text, meta) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  if (meta) {
    const metaDiv = document.createElement('div');
    metaDiv.className = 'meta';
    metaDiv.appendChild(meta);
    div.appendChild(metaDiv);
  }
  log.appendChild(div);
  window.scrollTo(0, document.body.scrollHeight);
  return div;
}

function metaLine(cached, sources) {
  const wrap = document.createElement('span');
  const badge = document.createElement('span');
  badge.className = 'badge ' + (cached ? 'cached' : 'miss');
  badge.textContent = cached ? 'cache hit' : 'cache miss';
  wrap.appendChild(badge);
  if (sources && sources.length) {
    wrap.appendChild(document.createTextNode('sources: ' + sources.join(', ')));
  } else {
    wrap.appendChild(document.createTextNode('no sources retrieved'));
  }
  return wrap;
}

async function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  const sessionId = localStorage.getItem(sessionKey);

  addMessage('user', text);
  input.value = '';
  sendBtn.disabled = true;
  statusEl.textContent = 'Sending...';

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        end_user_id: endUserId,
        session_id: sessionId ? Number(sessionId) : null,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      addMessage('error', 'HTTP ' + res.status + ': ' + (data.detail || JSON.stringify(data)));
    } else {
      localStorage.setItem(sessionKey, String(data.session_id));
      addMessage('assistant', data.answer, metaLine(data.cached, data.sources));
    }
  } catch (err) {
    addMessage('error', 'Request failed: ' + err.message);
  } finally {
    statusEl.textContent = '';
    sendBtn.disabled = false;
    input.focus();
  }
}

sendBtn.addEventListener('click', sendMessage);
input.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendMessage(); });
resetBtn.addEventListener('click', () => {
  localStorage.removeItem(sessionKey);
  log.innerHTML = '';
  statusEl.textContent = 'Started a new conversation.';
});
</script>
</body>
</html>
```

- [ ] **Step 3: Verify by hand against the real chat service**

From `webapp/`, with the venv active:

```bash
uvicorn app:app --port 8001 &
sleep 2
```

No credentials at all:
```bash
curl -s -o /dev/null -w "no creds: %{http_code}\n" http://localhost:8001/
```
Expected: `401`.

Wrong credentials:
```bash
curl -s -o /dev/null -w "wrong creds: %{http_code}\n" -u wrong:wrong http://localhost:8001/
```
Expected: `401`.

Correct credentials (read them out of `webapp/.env`, don't hardcode):
```bash
source .env
curl -s -o /dev/null -w "right creds, GET /: %{http_code}\n" -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" http://localhost:8001/
```
Expected: `200`.

A real end-to-end chat call through the proxy:
```bash
curl -s -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"webapp-task2-verify","session_id":null}'
```
Expected: `200`, a real grounded answer with `sources` and `cached: false`
in the body — proving the proxy correctly injected the real API key
server-side and forwarded a genuine request to the live chat service.

Confirm the real API key never appears anywhere in that response:
```bash
curl -s -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"webapp-task2-verify","session_id":null}' \
  | grep -c "$CHATSERVICE_API_KEY" || echo "confirmed: key not present in response"
```
Expected: `confirmed: key not present in response` (grep finds zero
matches, so its non-zero exit status triggers the `||` branch).

Confirm the served frontend HTML has no key/field for it either:
```bash
curl -s -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" http://localhost:8001/ | grep -ci "apikey\|api-key\|api_key" || echo "confirmed: no API key field in the page"
```
Expected: `confirmed: no API key field in the page`.

Now drive the lockout end-to-end through the real running server (5 wrong
attempts, then confirm even correct creds are rejected):
```bash
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "wrong attempt $i: %{http_code}\n" -u "wrong:wrong$i" http://localhost:8001/
done
curl -s -o /dev/null -w "correct creds after lockout: %{http_code}\n" -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" http://localhost:8001/
```
Expected: the first 4 attempts return `401`, the 5th returns `429` (the
lockout trips on the 5th), and the final correct-credentials request also
returns `429` (still locked out for the next ~5 minutes). Do not wait out
the real 5-minute window in this task — Task 1's own direct-function test
already proved the unlock path works; this step only needs to prove the
lockout blocks a correct login while active, through the real server.

Clean up the test session this created on the real chat service. This
reaches into the main service's `db.py` and its `DATABASE_URL`, which
lives in the main repo's *root* `.env` — `db.py`'s `config.py` calls
`load_dotenv()`, which reads `.env` from the current working directory, so
this must run from the repo root (not from `webapp/`), using the repo
root's own `.venv` (which already has `psycopg` installed; `webapp/.venv`
deliberately doesn't):
```bash
kill %1
cd ..
source .venv/bin/activate
python3 -c "
import db
db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = %s)\", ('webapp-task2-verify',))
db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = %s\", ('webapp-task2-verify',))
"
deactivate
cd webapp
```

- [ ] **Step 4: Commit**

```bash
git add webapp/app.py webapp/static/index.html
git commit -m "Add test webapp's FastAPI proxy and frontend"
```

---

## Task 3: Dockerfile and deploy scaffolding

**Files:**
- Create: `webapp/Dockerfile`
- Create: `webapp/.dockerignore`

**Interfaces:** None — packaging only, no code.

- [ ] **Step 1: Create `webapp/Dockerfile`**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT at runtime -- default it for local `docker run` testing.
ENV PORT=8000
EXPOSE 8000

# Shell form (not exec/JSON form) so $PORT actually gets expanded.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
```

- [ ] **Step 2: Create `webapp/.dockerignore`**

```
.venv/
__pycache__/
*.pyc
.env
```

- [ ] **Step 3: Verify by hand**

Check whether Docker is available in this environment:
```bash
command -v docker
```

If it prints a path, build and smoke-test the image for real:
```bash
cd webapp
docker build -t webapp-test .
docker run --rm -d -p 8002:8000 \
  -e WEBAPP_USERNAME=dockertest -e WEBAPP_PASSWORD=dockertest \
  -e CHATSERVICE_URL=https://app-production-cc74.up.railway.app \
  -e CHATSERVICE_API_KEY="$(grep CHATSERVICE_API_KEY .env | cut -d= -f2)" \
  --name webapp-test-container webapp-test
sleep 2
curl -s http://localhost:8002/health
curl -s -o /dev/null -w "auth check via docker: %{http_code}\n" -u dockertest:dockertest http://localhost:8002/
docker stop webapp-test-container
```
Expected: `/health` returns `{"status":"ok"}`, the authenticated `GET /`
returns `200`.

If `command -v docker` printed nothing (Docker not available in this
environment), skip the build/run and say so plainly in the report — do
not claim the image builds without actually having built it. The
Dockerfile's correctness will still be proven for real when Railway builds
it in Task 4.

- [ ] **Step 4: Commit**

```bash
git add webapp/Dockerfile webapp/.dockerignore
git commit -m "Add test webapp's Dockerfile"
```

---

## Task 4: Deploy and verify live (controller, not a subagent — needs Railway MCP tools and must handle real credentials directly)

**Files:** None (deploy + verification only).

**Interfaces:** None.

- [ ] **Step 1: Push to trigger nothing yet — the new service doesn't exist until created**

```bash
git push origin main
```

- [ ] **Step 2: Create the new Railway service**

Using the Railway CLI/MCP (already authenticated this session, project id
`b5d5a575-6ea3-4e92-980d-9905f84fe872`), create a new service named
`webapp`, connected to the same GitHub repo (`Rectifier09/MamaRoo-ChatService`,
branch `main`) as a Dockerfile-builder service, with `rootDirectory` set to
`webapp` so Railway builds only that subdirectory as its own context
(this is exactly the mechanism this session's Redis Phase 2 work has
already used for railway service management — see
`docs/superpowers/specs/2026-09-15-test-webapp-design.md`'s "Deployment"
section for the exact rootDirectory reasoning).

- [ ] **Step 3: Set the new service's variables**

Set on the `webapp` service (not the main `app` service):
- `WEBAPP_USERNAME` / `WEBAPP_PASSWORD` — the exact same values already in
  `webapp/.env` from the Prerequisite step (don't regenerate — the
  controller already has these).
- `CHATSERVICE_URL=http://${{app.RAILWAY_PRIVATE_DOMAIN}}` — Railway's
  internal service-to-service networking (same pattern already used for
  `${{Postgres.DATABASE_URL}}` elsewhere in this project), so
  server-to-server traffic between the two services in the same
  project/environment never leaves Railway's private network.
- `CHATSERVICE_API_KEY` — the same `pk_...` value from the Prerequisite
  step.

- [ ] **Step 4: Wait for the deploy to succeed**

Poll the new service's deployments until `status` is `SUCCESS`. Don't
proceed to live verification before this.

- [ ] **Step 5: Generate a public domain**

Generate a Railway public domain for the `webapp` service so it's
reachable from a browser.

- [ ] **Step 6: Verify live**

Let `LIVE_URL` be the domain generated in Step 5. From the repo root, with
`webapp/.env` sourced for the credentials/key:

```bash
export $(grep -v '^#' webapp/.env | xargs)

curl -s -o /dev/null -w "no creds: %{http_code}\n" "$LIVE_URL/"
curl -s -o /dev/null -w "wrong creds: %{http_code}\n" -u wrong:wrong "$LIVE_URL/"
curl -s -o /dev/null -w "right creds, GET /: %{http_code}\n" -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" "$LIVE_URL/"

curl -s -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" -X POST "$LIVE_URL/chat" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is Braxton Hicks?","end_user_id":"webapp-task4-live-verify","session_id":null}' \
  | tee /tmp/webapp-live-chat-response.json

grep -c "$CHATSERVICE_API_KEY" /tmp/webapp-live-chat-response.json || echo "confirmed: key not present in live response"

for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "wrong attempt $i: %{http_code}\n" -u "wrong:wrong$i" "$LIVE_URL/"
done
curl -s -o /dev/null -w "correct creds after lockout: %{http_code}\n" -u "$WEBAPP_USERNAME:$WEBAPP_PASSWORD" "$LIVE_URL/"
```

Expected: `401`, `401`, `200`, then a `200` with a real grounded answer
for the chat call, `confirmed: key not present in live response`, then
`401`×4 / `429` for the lockout burst, and `429` for the final
correct-credentials check (still locked out).

Clean up the live test session's `chat_sessions`/`chat_messages` row
afterward, from the main repo root using its own `.venv`:
```bash
source .venv/bin/activate
python3 -c "
import db
db.execute(\"DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = %s)\", ('webapp-task4-live-verify',))
db.execute(\"DELETE FROM chat_sessions WHERE end_user_id = %s\", ('webapp-task4-live-verify',))
"
deactivate
```
(Use `webapp-task4-live-verify` as this step's `end_user_id` in the live
`POST /chat` call above, so this cleanup targets exactly that row.)

- [ ] **Step 7: Report the result**

State plainly: the live URL, confirmation every check above passed for
real (not just "looks deployed"), and hand the `WEBAPP_USERNAME`/
`WEBAPP_PASSWORD` values to the user directly in the response (not
committed anywhere in git) so they can actually log in and use it.
