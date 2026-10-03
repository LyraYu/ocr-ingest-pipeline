from uuid import UUID

import psycopg

from app.db.connection import fetch_one

INSERT_COLUMNS = (
    "content_hash", "source_sha256", "upload_filename", "source_filename", "source_mime_type",
    "size_bytes", "raw_storage_uri", "country_code", "ocr_engine", "ocr_engine_version",
    "ocr_processed_at", "status", "error_code", "error_message", "latest_run_id",
)


def get(conn: psycopg.Connection, document_id: UUID) -> dict | None:
    return fetch_one(conn, "select * from documents where id = %s", (document_id,))


def get_by_content_hash(conn: psycopg.Connection, content_hash: str) -> dict | None:
    return fetch_one(conn, "select * from documents where content_hash = %s", (content_hash,))


def insert(conn: psycopg.Connection, **values) -> UUID | None:
    """Insert a document; None when content_hash already exists (concurrent duplicate)."""
    unknown = set(values) - set(INSERT_COLUMNS)
    if unknown:
        raise ValueError(f"unknown documents columns {sorted(unknown)}")
    columns = list(values)
    row = conn.execute(
        f"""
        insert into documents ({", ".join(columns)})
        values ({", ".join(["%s"] * len(columns))})
        on conflict (content_hash) do nothing
        returning id
        """,
        [values[c] for c in columns],
    ).fetchone()
    return row[0] if row else None


def set_status(conn: psycopg.Connection, document_id: UUID, status: str, run_id: UUID) -> None:
    conn.execute(
        """
        update documents
           set status = %s, error_code = null, error_message = null,
               latest_run_id = %s, updated_at = now()
         where id = %s
        """,
        (status, run_id, document_id),
    )


def mark_failed(
    conn: psycopg.Connection, document_id: UUID, error_code: str, error_message: str, run_id: UUID
) -> None:
    conn.execute(
        """
        update documents
           set status = 'failed', error_code = %s, error_message = %s,
               latest_run_id = %s, updated_at = now()
         where id = %s
        """,
        (error_code, error_message, run_id, document_id),
    )


def set_normalised_uri(conn: psycopg.Connection, document_id: UUID, uri: str) -> None:
    conn.execute(
        "update documents set normalised_storage_uri = %s, updated_at = now() where id = %s",
        (uri, document_id),
    )


def set_document_type(conn: psycopg.Connection, document_id: UUID, document_type: str | None) -> None:
    conn.execute(
        "update documents set document_type = %s, updated_at = now() where id = %s",
        (document_type, document_id),
    )
