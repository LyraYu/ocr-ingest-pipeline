from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.embedding import to_pgvector


def insert_many(
    conn: psycopg.Connection,
    run_id: UUID,
    model_name: str,
    model_version: str,
    dimension: int,
    rows,
) -> None:
    """`rows`: (chunk_id, vector). Existing rows for the same (chunk, model, version) are kept."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into chunk_embeddings (chunk_id, model_name, model_version, dimension, embedding, run_id)
            values (%s, %s, %s, %s, %s::vector, %s)
            on conflict (chunk_id, model_name, model_version) do nothing
            """,
            [(chunk_id, model_name, model_version, dimension, to_pgvector(vec), run_id) for chunk_id, vec in rows],
        )


def search(
    conn: psycopg.Connection,
    query_vector: list[float],
    model_name: str,
    model_version: str,
    chunking_version: str,
    top_k: int,
    document_type: str | None = None,
    country_code: str | None = None,
) -> list[dict]:
    """Nearest chunks by cosine distance (HNSW index), only rows of the given model.

    Filters (model, document type, country) are applied while scanning the index:
    `hnsw.iterative_scan = relaxed_order` (pgvector >= 0.8) keeps scanning until
    top_k rows pass the filters, instead of returning fewer. relaxed_order may
    return rows slightly out of order, so the outer query re-sorts by distance.
    """
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("set local hnsw.iterative_scan = relaxed_order")
        cur.execute(
            """
            select * from (
                select c.id as chunk_id, c.document_id, d.document_type, p.page_number as page,
                       e.embedding <=> %(q)s::vector as distance, c.text,
                       c.bbox_x0::float8 as x0, c.bbox_y0::float8 as y0,
                       c.bbox_x1::float8 as x1, c.bbox_y1::float8 as y1,
                       e.model_name
                  from chunk_embeddings e
                  join chunks c on c.id = e.chunk_id
                  join documents d on d.id = c.document_id
                  join document_pages p on p.id = c.page_id
                 where e.model_name = %(model)s and e.model_version = %(version)s
                   and c.chunking_version = %(chunking)s
                   and d.status = 'embedded'
                   and (%(doc_type)s::text is null or d.document_type = %(doc_type)s)
                   and (%(country)s::text is null or d.country_code = %(country)s)
                 order by e.embedding <=> %(q)s::vector
                 limit %(k)s
            ) hits
            order by distance
            """,
            {
                "q": to_pgvector(query_vector), "model": model_name, "version": model_version,
                "chunking": chunking_version, "doc_type": document_type, "country": country_code,
                "k": top_k,
            },
        )
        return cur.fetchall()
