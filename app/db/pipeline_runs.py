from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


def create(
    conn: psycopg.Connection,
    run_type: str,
    normaliser_version: str,
    code_version: str,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> UUID:
    return conn.execute(
        """
        insert into pipeline_runs
            (run_type, normaliser_version, code_version, embedding_model, embedding_model_version)
        values (%s, %s, %s, %s, %s)
        returning id
        """,
        (run_type, normaliser_version, code_version, embedding_model, embedding_model_version),
    ).fetchone()[0]


def finish(conn: psycopg.Connection, run_id: UUID, status: str, stats: dict) -> None:
    conn.execute(
        "update pipeline_runs set status = %s, stats = %s, finished_at = now() where id = %s",
        (status, Jsonb(stats), run_id),
    )
