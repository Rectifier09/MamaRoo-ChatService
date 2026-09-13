"""
Small Postgres helper: a shared connection pool with pgvector types registered,
plus three convenience functions used everywhere else in the app.
"""
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

import config


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=5,
    configure=_configure,
    kwargs={"autocommit": True},
)


def fetchone(query: str, params: tuple = ()):
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchone()


def fetchall(query: str, params: tuple = ()):
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()


def execute(query: str, params: tuple = ()):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
