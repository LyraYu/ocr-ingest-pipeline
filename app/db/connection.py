from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url, **kwargs)


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchone()


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()
