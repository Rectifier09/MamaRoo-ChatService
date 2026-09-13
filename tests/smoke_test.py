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

Streaming (`stream: true`) coverage: the happy path over HTTP (cache miss,
cache hit, multi-turn, persistence), pre-stream error parity with the
non-streaming path, and — in-process against the real generator — the
post-first-delta failure paths that can't be triggered against a live server
(see check_post_delta_failure_paths).
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


def chat_stream(
    api_key: str,
    message: str,
    end_user_id: str,
    session_id: int | None = None,
    origin: str | None = None,
):
    """POSTs to /chat with stream=true and reads the SSE response. Returns
    (events, status_code, body_text) -- events is every parsed `data:` JSON
    payload in order (empty if the response was a pre-stream JSON error
    instead), and body_text is the raw error body on a non-200 (empty string
    on a 200, where the payloads are in `events`). The body is returned, not
    discarded, because retry_on_transient_503_stream below needs it: a
    pre-stream provider 503 is a plain JSON body with no SSE events at all, so
    checking `events` for it could never match."""
    _created_end_users.append(end_user_id)
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    events = []
    with httpx.stream(
        "POST",
        f"{BASE_URL}/chat",
        headers=headers,
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
            return events, status_code, resp.read().decode()
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events, status_code, ""


def retry_on_transient_503_stream(fn, attempts: int = 3, delay: float = 8.0):
    """Same retry logic (and same semantics: retry only on 500 + UNAVAILABLE)
    as retry_on_transient_503, adapted for chat_stream's (events, status_code,
    body_text) return shape instead of an httpx.Response."""
    events, status, body = fn()
    tries = 1
    while status == 500 and "UNAVAILABLE" in body and tries < attempts:
        time.sleep(delay)
        events, status, body = fn()
        tries += 1
    return events, status, body


def check_post_delta_failure_paths() -> None:
    """Exercises generate_answer_stream's post-first-delta failure paths directly,
    in-process, against the real generator, the real DB and (for the cache-write
    case) a real Groq stream.

    Why in-process rather than over HTTP: these paths need a failure injected at
    a specific point mid-generation. The live server is a separate process, so
    there is no way to make a real Groq stream fail on demand after N chunks, and
    no reliable way to make a real DB write fail without breaking every other
    check in this file. Injecting the failure into the real generator here tests
    the actual code app.py drives -- only the failure trigger is synthetic, not
    the code under test. The counterpart in app.py's event_stream (the terminal
    event it emits when a persist fails) is the same two-line pattern.

    Must run while qa_cache is empty -- a cache hit would short-circuit
    generate_answer_stream before it ever reaches the provider.
    """
    # Imported here rather than at module scope: importing rag_engine loads the
    # local embedding model (sentence-transformers/torch), several seconds of
    # startup cost that only these three checks need.
    import cache as cache_module
    import llm_provider
    import rag_engine

    def cache_rows() -> int:
        return db.fetchone("SELECT count(*) AS n FROM qa_cache")["n"]

    provider = llm_provider.get_provider(config.ANSWER_PROVIDER)

    # --- (a) provider raises AFTER real deltas were already yielded ---
    def exploding_stream(**kwargs):
        yield "Braxton Hicks "
        yield "contractions are "
        raise RuntimeError("simulated mid-stream provider failure")

    before = cache_rows()
    provider.stream_complete = exploding_stream
    try:
        events = list(rag_engine.generate_answer_stream("What is a birth plan?"))
    finally:
        del provider.stream_complete  # unshadow the real bound method

    deltas = [payload for kind, payload in events if kind == "delta"]
    dones = [payload for kind, payload in events if kind == "done"]
    check(
        "mid-stream provider failure: deltas delivered, then exactly one terminal error event",
        len(deltas) == 2
        and len(dones) == 1
        and events[-1][0] == "done"
        and "simulated mid-stream provider failure" in dones[0].get("error", ""),
        str(events),
    )
    check(
        "mid-stream provider failure: nothing written to qa_cache",
        cache_rows() == before,
        f"{before} -> {cache_rows()}",
    )

    # --- (b) provider completes cleanly but yields zero chunks ---
    def empty_stream(**kwargs):
        return iter(())

    before = cache_rows()
    provider.stream_complete = empty_stream
    try:
        events = list(rag_engine.generate_answer_stream("What is a doula?"))
    finally:
        del provider.stream_complete

    deltas = [payload for kind, payload in events if kind == "delta"]
    dones = [payload for kind, payload in events if kind == "done"]
    check(
        "zero-delta generation -> error done event, not a success done event",
        len(deltas) == 0 and len(dones) == 1 and "error" in dones[0],
        str(events),
    )
    check(
        "zero-delta generation does not cache an empty answer",
        cache_rows() == before,
        f"{before} -> {cache_rows()} (an empty cached answer would poison the shared "
        "qa_cache for the non-streaming path too)",
    )

    # --- (c) the cache write itself fails after a real, complete Groq stream ---
    original_store = cache_module.store_answer

    def exploding_store(*args, **kwargs):
        raise RuntimeError("simulated cache-write failure")

    cache_module.store_answer = exploding_store
    try:
        events = list(rag_engine.generate_answer_stream(REAL_QUESTION))
    finally:
        cache_module.store_answer = original_store

    deltas = [payload for kind, payload in events if kind == "delta"]
    dones = [payload for kind, payload in events if kind == "done"]
    check(
        "cache-write failure after a real stream still yields exactly one terminal error event",
        len(deltas) >= 1
        and len(dones) == 1
        and events[-1][0] == "done"
        and "simulated cache-write failure" in dones[0].get("error", ""),
        str(events)[:500],
    )


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

    # --- streaming: pre-stream errors behave identically to non-streaming ---
    # API_CONTRACT.md's SSE section promises that stream:true changes nothing
    # about pre-stream error behavior -- same status codes, same JSON body
    # shape, never a stream. Checked here (rather than down in the streaming
    # section) so the 429 case can reuse ratelimit_key's already-exhausted
    # window: a request that gets 429'd is rejected before the limiter records
    # it, so this adds nothing to that key's counters.
    _, empty_status, _ = chat_stream(main_key, "   ", "smoke-stream-empty-msg")
    check("POST /chat stream=true empty message -> 400 (same as non-streaming)", empty_status == 400)

    _, badkey_status, _ = chat_stream("pk_invalid_xyz", "hi", "smoke-stream-bad-key")
    check("POST /chat stream=true wrong API key -> 401 (same as non-streaming)", badkey_status == 401)

    _, badorigin_status, _ = chat_stream(
        origin_key, "hi", "smoke-stream-bad-origin", origin="https://not-allowed.example.com"
    )
    check("POST /chat stream=true disallowed origin -> 403 (same as non-streaming)", badorigin_status == 403)

    _, ratelimited_status, _ = chat_stream(ratelimit_key, "ping four", "smoke-stream-ratelimit")
    check(
        "POST /chat stream=true over rate limit -> 429 (same as non-streaming)",
        ratelimited_status == 429,
        f"got {ratelimited_status}",
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

    # --- streaming: post-first-delta failure paths (in-process, see docstring) ---
    check_post_delta_failure_paths()

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
    events, status, body = retry_on_transient_503_stream(
        lambda: chat_stream(main_key, REAL_QUESTION, stream_user)
    )
    check("POST /chat stream=true cache miss -> 200", status == 200, str(events) + body)
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
    events2, status2, body2 = chat_stream(main_key, REAL_QUESTION, stream_user)
    check("POST /chat stream=true cache hit -> 200", status2 == 200, str(events2) + body2)
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
    events3, status3, body3 = retry_on_transient_503_stream(
        lambda: chat_stream(main_key, FOLLOWUP, stream_user, session_id=stream_session_id)
    )
    check("POST /chat stream=true multi-turn -> 200", status3 == 200, str(events3) + body3)
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
