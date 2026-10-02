from uuid import UUID

import psycopg

from app.db.connection import fetch_all, fetch_one


def delete_for_document(conn: psycopg.Connection, document_id: UUID) -> None:
    """Cascades to ocr_lines and anything else hanging off the pages."""
    conn.execute("delete from document_pages where document_id = %s", (document_id,))


def insert(conn: psycopg.Connection, **values) -> UUID:
    return conn.execute(
        """
        insert into document_pages
            (document_id, page_number, width, height, size_unit, size_reason,
             ocr_engine, ocr_engine_version, mean_confidence, line_count, run_id)
        values (%(document_id)s, %(page_number)s, %(width)s, %(height)s, %(size_unit)s,
                %(size_reason)s, %(ocr_engine)s, %(ocr_engine_version)s, %(mean_confidence)s,
                %(line_count)s, %(run_id)s)
        returning id
        """,
        values,
    ).fetchone()[0]


def list_for_document(conn: psycopg.Connection, document_id: UUID) -> list[dict]:
    return fetch_all(
        conn,
        """
        select id, page_number, width, height, size_unit, size_reason, ocr_engine,
               ocr_engine_version, mean_confidence, line_count, run_id
          from document_pages
         where document_id = %s
         order by page_number
        """,
        (document_id,),
    )


def count_for_document(conn: psycopg.Connection, document_id: UUID) -> int:
    return fetch_one(
        conn, "select count(*) as n from document_pages where document_id = %s", (document_id,)
    )["n"]
