from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.db.connection import fetch_all


def list_for_document(conn: psycopg.Connection, document_id: UUID) -> list[dict]:
    return fetch_all(
        conn,
        """
        select field_name, raw_value, normalised_value, value_type, validation_status,
               validation_message, source_line_ids, run_id
          from extracted_fields
         where document_id = %s
         order by field_name
        """,
        (document_id,),
    )


def replace_for_document(conn: psycopg.Connection, document_id: UUID, run_id: UUID, rows: list[dict]) -> None:
    """Delete the document's fields and insert `rows` (keys: field_name, raw_value,
    normalised_value, value_type, validation_status, validation_message, source_line_ids)."""
    conn.execute("delete from extracted_fields where document_id = %s", (document_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into extracted_fields
                (document_id, field_name, raw_value, normalised_value, value_type,
                 validation_status, validation_message, source_line_ids, run_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (document_id, r["field_name"], r["raw_value"],
                 None if r["normalised_value"] is None else Jsonb(r["normalised_value"]),
                 r["value_type"], r["validation_status"], r["validation_message"],
                 r["source_line_ids"], run_id)
                for r in rows
            ],
        )


def delete_for_document(conn: psycopg.Connection, document_id: UUID) -> None:
    conn.execute("delete from extracted_fields where document_id = %s", (document_id,))
