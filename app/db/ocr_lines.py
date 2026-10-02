from uuid import UUID

import psycopg


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
