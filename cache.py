"""
Semantic cache over qa_cache. A hit skips the answer-generation LLM call entirely —
the biggest single cost lever in this service, since it's a 100% savings rather than
the ~90% Anthropic's own prompt caching gives on a cache hit.
"""
import db
import config


def find_cached_answer(question_embedding) -> dict | None:
    # ::vector casts are required here: psycopg has no destination-column type to
    # infer from in a bare `<=>` expression, so a plain Python list parameter
    # defaults to `double precision[]` and Postgres rejects `vector <=> double
    # precision[]` outright. INSERTs don't need this (the target column's type
    # supplies it), only comparisons like this one.
    row = db.fetchone(
        """
        SELECT id, answer, sources, canonical_question,
               (question_embedding <=> %s::vector) AS distance
        FROM qa_cache
        ORDER BY question_embedding <=> %s::vector
        LIMIT 1
        """,
        (question_embedding, question_embedding),
    )
    if row and row["distance"] <= config.CACHE_MAX_DISTANCE:
        db.execute("UPDATE qa_cache SET hit_count = hit_count + 1 WHERE id = %s", (row["id"],))
        return row
    return None


def store_answer(canonical_question: str, question_embedding, answer: str, sources: list[str]) -> None:
    db.execute(
        """
        INSERT INTO qa_cache (canonical_question, question_embedding, answer, sources)
        VALUES (%s, %s, %s, %s)
        """,
        (canonical_question, question_embedding, answer, sources),
    )
