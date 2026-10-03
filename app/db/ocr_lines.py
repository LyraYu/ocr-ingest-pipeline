from uuid import UUID

import psycopg

from app.db.connection import fetch_all


def insert_many(conn: psycopg.Connection, document_id: UUID, page_id: UUID, run_id: UUID, lines) -> None:
    """`lines`: iterable of app.ocr.schema.NormalisedLine."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into ocr_lines
                (document_id, page_id, line_index, text, bbox_x0, bbox_y0, bbox_x1, bbox_y1,
                 confidence, confidence_source, run_id)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (document_id, page_id, line.line_index, line.text, *line.bbox,
                 line.confidence, line.confidence_source, run_id)
                for line in lines
            ],
        )


def list_for_document(conn: psycopg.Connection, document_id: UUID) -> list[dict]:
    """All lines of a document in page, then reading, order."""
    return fetch_all(
        conn,
        """
        select l.id, p.page_number, l.line_index, l.text
          from ocr_lines l
          join document_pages p on p.id = l.page_id
         where l.document_id = %s
         order by p.page_number, l.line_index
        """,
        (document_id,),
    )
