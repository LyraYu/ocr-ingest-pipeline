from uuid import UUID

import psycopg

from app.db.connection import fetch_one


def count_for_document(conn: psycopg.Connection, document_id: UUID) -> int:
    return fetch_one(
        conn, "select count(*) as n from chunks where document_id = %s", (document_id,)
    )["n"]
