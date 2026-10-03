from uuid import UUID

import psycopg

from app.db.connection import fetch_all, fetch_one


def count_for_document(conn: psycopg.Connection, document_id: UUID, chunking_version: str) -> int:
    return fetch_one(
        conn,
        "select count(*) as n from chunks where document_id = %s and chunking_version = %s",
        (document_id, chunking_version),
    )["n"]


def insert_many(conn: psycopg.Connection, document_id: UUID, run_id: UUID, chunking_version: str, rows) -> None:
    """`rows`: (chunk_index, page_id, ChunkDraft)."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into chunks
                (document_id, page_id, chunk_index, chunking_version, text,
                 bbox_x0, bbox_y0, bbox_x1, bbox_y1, source_line_ids, char_count, run_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (document_id, page_id, index, chunking_version, draft.text, *draft.bbox,
                 draft.source_line_ids, draft.char_count, run_id)
                for index, page_id, draft in rows
            ],
        )


def missing_embeddings(
    conn: psycopg.Connection,
    document_ids: list[UUID],
    chunking_version: str,
    model_name: str,
    model_version: str,
) -> list[dict]:
    """Chunks of these documents with no embedding row for (model_name, model_version)."""
    return fetch_all(
        conn,
        """
        select c.id, c.document_id, c.text
          from chunks c
         where c.document_id = any(%s) and c.chunking_version = %s
           and not exists (
               select 1 from chunk_embeddings e
                where e.chunk_id = c.id and e.model_name = %s and e.model_version = %s)
         order by c.document_id, c.chunk_index
        """,
        (document_ids, chunking_version, model_name, model_version),
    )
