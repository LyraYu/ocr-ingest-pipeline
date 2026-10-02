from uuid import UUID

import psycopg

from app.db.connection import fetch_all


def list_for_document(conn: psycopg.Connection, document_id: UUID) -> list[dict]:
    return fetch_all(
        conn,
        """
        select q.check_name, q.passed, q.severity, q.details, p.page_number, q.run_id
          from quality_checks q
          left join document_pages p on p.id = q.page_id
         where q.document_id = %s
         order by q.check_name, p.page_number nulls first
        """,
        (document_id,),
    )


def failed_check_names(conn: psycopg.Connection, document_id: UUID) -> list[str]:
    rows = fetch_all(
        conn,
        """
        select distinct check_name from quality_checks
         where document_id = %s and not passed
         order by check_name
        """,
        (document_id,),
    )
    return [r["check_name"] for r in rows]
