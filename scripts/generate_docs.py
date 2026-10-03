"""Regenerate the report materials in docs/ from a fresh database.

    docker compose run --rm -v "$PWD/docs:/app/docs" api python -m scripts.generate_docs

Uses its own database `docs_report` (dropped and recreated on every run) and a
temporary data directory, so it never touches `docs` or ./data. Writes:

    docs/normalised/<document_type>.json         normalised OCR JSON per sample
    docs/api/post_documents_<document_type>.json  POST /documents response per sample
    docs/api/get_documents_<document_type>.json   GET /documents/{id} response per sample
    docs/api/search_<n>.json                      three searches, request + response
    docs/quarantine.txt                           v_quarantine after also ingesting tests/fixtures/
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SAMPLES = ROOT / "samples"
FIXTURES = ROOT / "tests" / "fixtures"
REPORT_DB = "docs_report"

SEARCHES = [
    {"query": "medical leave for acute gastroenteritis", "top_k": 3,
     "filters": {"document_type": "medical_certificate", "country_code": "SG"}},
    {"query": "knee pain referral to orthopaedic specialist", "top_k": 3},
    {"query": "GST amount on consultation receipt", "top_k": 3, "filters": {"document_type": "receipt"}},
]

QUARANTINE_QUERY = """
select upload_filename, status, error_code, document_type, country_code, failed_checks
  from v_quarantine
 order by upload_filename
"""


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def recreate_database(base_url: str) -> str:
    report_url = _with_database(base_url, REPORT_DB)
    with psycopg.connect(_with_database(base_url, "postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(REPORT_DB)))
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(REPORT_DB)))
    return report_url


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def psql_table(columns: list[str], rows: list[tuple]) -> str:
    """Aligned text table in psql's default style."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, list):
            return "{" + ",".join(v) + "}"
        return str(v)

    cells = [[cell(v) for v in row] for row in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    lines = [" " + " | ".join(c.center(w) for c, w in zip(columns, widths)),
             "-" + "-+-".join("-" * w for w in widths) + "-"]
    lines += [" " + " | ".join(v.ljust(w) for v, w in zip(r, widths)) for r in cells]
    lines.append(f"({len(rows)} rows)")
    return "\n".join(lines) + "\n"


def main() -> int:
    base_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = recreate_database(base_url)
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="docs-report-")

    # Imported after the environment points at the report database.
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from app.cli import app as cli_app
    from app.db.connection import connect
    from app.db.migrate import migrate
    from app.main import app
    from app.pipeline import storage

    migrate()
    client = TestClient(app)

    for sample in sorted(SAMPLES.glob("*.json")):
        response = client.post("/documents", files={"file": (sample.name, sample.read_bytes(), "application/json")})
        assert response.status_code == 201, response.text
        posted = response.json()
        doc_type = posted["document_type"]
        write_json(DOCS / "api" / f"post_documents_{doc_type}.json", posted)

        got = client.get(f"/documents/{posted['document_id']}")
        assert got.status_code == 200, got.text
        write_json(DOCS / "api" / f"get_documents_{doc_type}.json", got.json())

        uri = got.json()["normalised_storage_uri"]
        write_json(DOCS / "normalised" / f"{doc_type}.json", json.loads(storage.read_bytes(uri)))

    for n, request in enumerate(SEARCHES, start=1):
        response = client.post("/search", json=request)
        assert response.status_code == 200, response.text
        write_json(DOCS / "api" / f"search_{n}.json", {"request": request, "response": response.json()})

    result = CliRunner().invoke(cli_app, ["ingest", str(FIXTURES)])
    print(result.output)
    assert result.exit_code == 0, result.output

    with connect() as conn:
        cur = conn.execute(QUARANTINE_QUERY)
        table = psql_table([d.name for d in cur.description], cur.fetchall())
    (DOCS / "quarantine.txt").write_text(
        "-- docs/quarantine.txt: v_quarantine after ingesting samples/ (3 files) and tests/fixtures/ (4 files)\n"
        f"-- generated by scripts/generate_docs.py\n{QUARANTINE_QUERY.strip()};\n\n{table}",
        encoding="utf-8",
    )
    print("wrote docs/quarantine.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
