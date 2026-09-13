"""
Usage: python init_db.py
Applies schema.sql to the database at config.DATABASE_URL. Safe to re-run — every
statement in schema.sql uses IF NOT EXISTS.
"""
import psycopg

import config


def main():
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set (check your .env)")

    with open("schema.sql") as f:
        statements = [s.strip() for s in f.read().split(";") if s.strip()]

    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)

    print(f"Applied {len(statements)} statement(s) from schema.sql")


if __name__ == "__main__":
    main()
