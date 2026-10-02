from datetime import datetime
from uuid import UUID

import psycopg

from app.db.connection import fetch_all


def record(
    conn: psycopg.Connection,
    document_id: UUID,
    stage: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    run_id: UUID,
    error: str | None = None,
) -> None:
    """One row per (document, stage, run); re-running a stage in the same run overwrites it."""
    conn.execute(
        """
        insert into document_stages
            (document_id, stage, status, started_at, finished_at, duration_ms, error, run_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (document_id, stage, run_id) do update
           set status = excluded.status, started_at = excluded.started_at,
               finished_at = excluded.finished_at, duration_ms = excluded.duration_ms,
               error = excluded.error
        """,
        (document_id, stage, status, started_at, finished_at, duration_ms, error, run_id),
    )


def list_for_document(conn: psycopg.Connection, document_id: UUID) -> list[dict]:
    return fetch_all(
        conn,
        """
        select stage, status, started_at, finished_at, duration_ms, error, run_id
          from document_stages
         where document_id = %s
         order by started_at, finished_at
        """,
        (document_id,),
    )


def latest_durations(conn: psycopg.Connection, document_id: UUID) -> dict[str, int]:
    """stage → duration_ms of that stage's most recent successful execution."""
    rows = fetch_all(
        conn,
        """
        select distinct on (stage) stage, duration_ms
          from document_stages
         where document_id = %s and status = 'succeeded'
         order by stage, finished_at desc
        """,
        (document_id,),
    )
    return {r["stage"]: r["duration_ms"] for r in rows}
