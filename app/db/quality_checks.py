from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

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
         where document_id = %s and not passed and severity in ('error', 'warning')
         order by check_name
        """,
        (document_id,),
    )
    return [r["check_name"] for r in rows]


def replace(
    conn: psycopg.Connection,
    document_id: UUID,
    run_id: UUID,
    owned_names: list[str],
    checks: list[dict],
) -> None:
    """Delete the document's checks whose name matches one of `owned_names` (SQL LIKE
    patterns, e.g. 'field\\_%'), then insert `checks` (keys: check_name, passed,
    severity, details, page_id optional). Each stage owns its check names, so
    re-running one stage leaves the other stages' checks alone."""
    conn.execute(
        "delete from quality_checks where document_id = %s and check_name like any(%s)",
        (document_id, owned_names),
    )
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into quality_checks (document_id, page_id, check_name, passed, severity, details, run_id)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (document_id, c.get("page_id"), c["check_name"], c["passed"], c["severity"],
                 Jsonb(c.get("details") or {}), run_id)
                for c in checks
            ],
        )
