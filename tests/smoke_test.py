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
"""
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
    check(
        "multi-turn follow-up resolves against history",
        resp.status_code == 200 and "braxton" in resp.json().get("answer", "").lower(),
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
