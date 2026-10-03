"""The bad-input fixtures in tests/fixtures/ (also used by scripts/generate_docs.py):

| file                    | expected outcome                                                  |
|-------------------------|-------------------------------------------------------------------|
| junk.txt                | 422 unreadable_file (not JSON)                                    |
| unknown_engine.json     | 422 unsupported_ocr_format (ocr.engine = abbyy-finereader)        |
| no_document_type.json   | 422 unsupported_document_type (receipt with every word replaced)  |
| low_confidence_mc.json  | accepted, embedded, flagged page_confidence (confidence × 0.62)   |

Ingesting them together with the three samples must quarantine exactly the four
fixtures and none of the samples. Also covers v_documents_deidentified.
"""

import shutil

from typer.testing import CliRunner

from app.cli import app as cli_app
from tests.conftest import FIXTURES_DIR, SAMPLES_DIR

EXPECTED = {
    "junk.txt": ("failed", "unreadable_file", ["file_json"]),
    "unknown_engine.json": ("failed", "unsupported_ocr_format", ["engine_supported"]),
    "no_document_type.json": ("failed", "unsupported_document_type", ["document_type_supported"]),
    "low_confidence_mc.json": ("embedded", None, ["page_confidence"]),
}


def test_fixtures_are_quarantined_and_samples_are_not(db_conn, tmp_path):
    for folder in (SAMPLES_DIR, FIXTURES_DIR):
        for path in folder.iterdir():
            shutil.copy(path, tmp_path / path.name)

    result = CliRunner().invoke(cli_app, ["ingest", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "processed: 4  duplicate: 0  failed: 3" in result.output
    rows = db_conn.execute(
        "select upload_filename, status, error_code, failed_checks from v_quarantine"
    ).fetchall()
    assert {name: (status, code, checks) for name, status, code, checks in rows} == EXPECTED


def test_deidentified_view_excludes_claimant_fields(db_conn):
    # Relies on the documents ingested by the previous test (same module).
    fields = {r[0] for r in db_conn.execute("select distinct field_name from v_documents_deidentified")}
    assert fields  # amounts, dates, provider_name, ...
    assert not {"claimant_name", "claimant_address", "claimant_date_of_birth"} & fields
    assert {"provider_name", "total_amount", "mc_days"} <= fields
    columns = [d.name for d in db_conn.execute("select * from v_documents_deidentified limit 0").description]
    assert "raw_value" not in columns and "upload_filename" not in columns
