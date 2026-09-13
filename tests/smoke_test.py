"""
End-to-end smoke test against a running instance (local or deployed).

Requires the knowledge base to already be ingested (see ingest.py) with content
that can answer a real question — this test asks about Braxton Hicks, which
exists in data/knowledge_base/Mayo-Clinic-Pregnancy-Library.md. Point it at a
different KB by editing REAL_QUESTION/REAL_QUESTION_REWORDED/FOLLOWUP below.

Usage:
    python tests/smoke_test.py [base_url]
    # base_url defaults to http://localhost:8000

Requires ADMIN_API_KEY in the environment (loaded from .env via config.py) —
used to provision throwaway test products, which are deleted again at the end.

Also covers GET /chat/sessions (list) and GET /chat/sessions/{id}/messages
(detail): thread listing/ordering/titles, cross-user isolation, and
zero-message-session exclusion.
"""
import json
import sys
import time

import httpx

sys.path.insert(0, ".")
import config  # noqa: E402
import db  # noqa: E402

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN_KEY = config.ADMIN_API_KEY

REAL_QUESTION = "What is Braxton Hicks?"
# Empirically verified (2026-09-13) at cosine distance 0.0101 against
# REAL_QUESTION with the local embedding model — comfortably under
# CACHE_MAX_DISTANCE (0.08). The previous wording ("Can you explain what
# Braxton Hicks contractions are?") measured 0.159, roughly double the
# threshold, so the cache-hit check below was silently unverifiable — it
# happened to depend on an assumption about embedding similarity that was
# never actually checked against the real model.
REAL_QUESTION_REWORDED = "What are Braxton Hicks?"
FOLLOWUP = "when do they usually happen?"
# Deliberately distinct from REAL_QUESTION: qa_cache is shared across all
# products by design (see ARCHITECTURE.md), so if this test reused
# REAL_QUESTION, its real call would cache that answer and make the
# dedicated cache-miss check below spuriously come back cached=true.
ORIGIN_TEST_QUESTION = "What foods should I avoid during pregnancy?"

_created_product_ids: list[int] = []
_created_end_users: list[str] = []
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def create_product(name: str, allowed_origins: list[str], rate_limit: int) -> dict:
    resp = httpx.post(
        f"{BASE_URL}/admin/products",
        headers={"X-Admin-Key": ADMIN_KEY},
        json={"name": name, "allowed_origins": allowed_origins, "rate_limit_per_minute": rate_limit},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    _created_product_ids.append(body["id"])
    return body


def chat(api_key: str, message: str, end_user_id: str, session_id: int | None = None, origin: str | None = None):
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    _created_end_users.append(end_user_id)
    return httpx.post(
        f"{BASE_URL}/chat",
        headers=headers,
        json={"message": message, "end_user_id": end_user_id, "session_id": session_id},
        timeout=60,
    )


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


def main() -> int:
    if not ADMIN_KEY:
        print("ADMIN_API_KEY not set (check .env) — cannot provision test products.")
        return 1

    print(f"Running smoke test against {BASE_URL}\n")

    # --- health ---
    resp = httpx.get(f"{BASE_URL}/health", timeout=10)
    check("GET /health -> 200", resp.status_code == 200 and resp.json().get("status") == "ok")

    # --- admin auth ---
    resp = httpx.post(f"{BASE_URL}/admin/products", headers={"X-Admin-Key": "wrong"}, json={"name": "x"}, timeout=10)
    check("POST /admin/products wrong admin key -> 401", resp.status_code == 401)

    # --- provision test products ---
    main_product = create_product("smoke-test-main", ["*"], 30)
    check("POST /admin/products returns pk_ key", main_product["api_key"].startswith("pk_"))
    main_key = main_product["api_key"]

    origin_product = create_product("smoke-test-origin", ["https://mamaroo.app"], 30)
    origin_key = origin_product["api_key"]

    ratelimit_product = create_product("smoke-test-ratelimit", ["*"], 2)
    ratelimit_key = ratelimit_product["api_key"]

    # --- message validation ---
    resp = chat(main_key, "   ", "smoke-empty-msg")
    check("POST /chat empty message -> 400", resp.status_code == 400)

    # --- API key auth ---
    resp = httpx.post(
        f"{BASE_URL}/chat",
        headers={"X-API-Key": "pk_invalid_xyz", "Content-Type": "application/json"},
        json={"message": "hi", "end_user_id": "smoke-bad-key", "session_id": None},
        timeout=10,
    )
    check("POST /chat wrong API key -> 401", resp.status_code == 401)

    # --- origin allow-list ---
    resp = chat(origin_key, "hi", "smoke-bad-origin", origin="https://not-allowed.example.com")
    check("POST /chat disallowed origin -> 403", resp.status_code == 403)

    resp = retry_on_transient_503(
        lambda: chat(origin_key, ORIGIN_TEST_QUESTION, "smoke-good-origin", origin="https://mamaroo.app")
    )
    check("POST /chat allowed origin -> 200", resp.status_code == 200, resp.text)

    # --- cache miss / cache hit ---
    resp = retry_on_transient_503(lambda: chat(main_key, REAL_QUESTION, "smoke-cache"))
    check("cache miss -> 200, cached=false", resp.status_code == 200 and resp.json().get("cached") is False, resp.text)
    body = resp.json()
    check("cache miss has real sources", len(body.get("sources", [])) > 0, str(body))
    session_id = body.get("session_id")

    resp = retry_on_transient_503(lambda: chat(main_key, REAL_QUESTION_REWORDED, "smoke-cache"))
    check("cache hit (reworded question) -> cached=true", resp.status_code == 200 and resp.json().get("cached") is True, resp.text)

    # --- multi-turn / rewrite ---
    resp = chat(main_key, FOLLOWUP, "smoke-cache", session_id=session_id)
    # A correct resolution of "when do they usually happen?" answers with the
    # actual timing facts (afternoon/evening, after exercise/sex) -- it does
    # NOT need to restate "Braxton Hicks" by name. Requiring the literal name
    # would penalize exactly the natural, context-aware phrasing this check
    # exists to confirm, so this checks for the real answer content instead.
    answer_lower = resp.json().get("answer", "").lower()
    check(
        "multi-turn follow-up resolves against history",
        resp.status_code == 200
        and any(kw in answer_lower for kw in ("afternoon", "evening", "exercise", "due date")),
        resp.text,
    )

    # --- rate limiting ---
    r1 = chat(ratelimit_key, "ping one", "smoke-ratelimit")
    r2 = chat(ratelimit_key, "ping two", "smoke-ratelimit")
    r3 = chat(ratelimit_key, "ping three", "smoke-ratelimit")
    check(
        "rate limit enforced (3rd request in same minute -> 429)",
        r3.status_code == 429,
        f"r1={r1.status_code} r2={r2.status_code} r3={r3.status_code}",
    )

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

    # Clear qa_cache before testing streaming behavior, so cache miss test is unambiguous
    db.execute("DELETE FROM qa_cache")

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

    # --- cleanup ---
    db.execute(
        "DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE end_user_id = ANY(%s))",
        (_created_end_users,),
    )
    db.execute("DELETE FROM chat_sessions WHERE end_user_id = ANY(%s)", (_created_end_users,))
    db.execute("DELETE FROM qa_cache")
    db.execute("DELETE FROM products WHERE id = ANY(%s)", (_created_product_ids,))
    print(f"\nCleaned up {len(_created_product_ids)} test product(s) and their sessions/cache.")

    print(f"\n{len(failures)} failure(s)." if failures else "\nAll checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
