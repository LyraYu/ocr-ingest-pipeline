from uuid import UUID

import psycopg

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
