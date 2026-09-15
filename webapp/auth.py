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

    # Reject all logins if credentials aren't configured. This prevents a
    # credential bypass in misconfigured deployments (empty username/password
    # would otherwise pass secrets.compare_digest("", "") and authenticate).
    if not config.WEBAPP_USERNAME or not config.WEBAPP_PASSWORD:
        raise HTTPException(status_code=401, detail="Webapp is not configured (missing credentials)")

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
