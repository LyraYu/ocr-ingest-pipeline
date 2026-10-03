"""Phase 2: receive → normalise → load through the API, runner and CLI."""

import json
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.ocr import NORMALISER_VERSION
from app.pipeline import storage
from app.pipeline.runner import run_pipeline, start_run
from app.pipeline.stages import stage_load


def variant(sample: bytes, tag: str) -> bytes:
    """Same export, different bytes (an ignored extra envelope key), so tests in one
    module don't see each other's uploads as duplicates."""
    data = json.loads(sample)
    data["test_variant"] = tag
    return json.dumps(data).encode()


def upload(client, data: bytes, filename: str = "upload.json", **form):
    return client.post("/documents", files={"file": (filename, data, "application/json")}, data=form)


def count(conn, sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()[0]


def test_idempotent_ingest(db_conn, client, sample_bytes_by_engine):
    data = variant(sample_bytes_by_engine["aws-textract"], "idempotent")

    first = upload(client, data, "first_name.json")
    second = upload(client, data, "second_name.json")

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    a, b = first.json(), second.json()
    assert a["duplicate"] is False and b["duplicate"] is True
    assert a["document_id"] == b["document_id"]
    assert a["content_hash"] == b["content_hash"]
    assert a["content_hash"].startswith("sha256:")
    assert a["status"] == b["status"] == "embedded"
    assert a["pages"] == b["pages"] == 1
    assert a["document_type"] == b["document_type"] == "medical_certificate"
    assert a["chunks"] == b["chunks"] > 0 and a["quality_flags"] == [] and b["quality_flags"] == []
    assert set(a["timings_ms"]) == {"normalise", "load", "embed"}
    assert a["timings_ms"]["normalise"] >= 0 and a["timings_ms"]["load"] >= 0
    assert a["timings_ms"]["embed"] >= 0

    doc_id = a["document_id"]
    assert count(db_conn, "select count(*) from documents where content_hash = %s", a["content_hash"]) == 1
    assert count(db_conn, "select upload_filename = 'first_name.json' from documents where id = %s", doc_id)
    assert count(db_conn, "select count(*) from document_pages where document_id = %s", doc_id) == 1
    assert count(db_conn, "select count(*) from ocr_lines where document_id = %s", doc_id) == 31
    assert count(db_conn, "select count(*) from document_stages where document_id = %s", doc_id) == 7
    assert count(db_conn, "select count(*) from extracted_fields where document_id = %s", doc_id) == 10
    chunk_count = count(db_conn, "select count(*) from chunks where document_id = %s", doc_id)
    assert chunk_count == a["chunks"]
    assert count(db_conn, "select count(*) from chunk_embeddings e join chunks c on c.id = e.chunk_id"
                          " where c.document_id = %s", doc_id) == chunk_count
    # The duplicate created no pipeline run either.
    assert count(db_conn, "select count(distinct run_id) from document_stages where document_id = %s", doc_id) == 1


def test_rejections(db_conn, client, isolated_data_dir, samples_by_engine):
    # Non-JSON bytes → 422 unreadable_file, stored in the raw zone with a failed row.
    junk = b"%PDF-1.7 definitely not json " + uuid4().bytes
    response = upload(client, junk, "scan.pdf")
    assert response.status_code == 422
    assert response.json() == {"error": "unreadable_file"}
    row = db_conn.execute(
        "select status, error_code, raw_storage_uri, country_code from documents where upload_filename = 'scan.pdf'"
    ).fetchone()
    assert row[:2] == ("failed", "unreadable_file")
    assert storage.uri_to_path(row[2]).read_bytes() == junk
    assert str(storage.uri_to_path(row[2])).startswith(str(isolated_data_dir / "raw"))
    assert row[3] == "SG"

    # Valid JSON, unknown engine → 422 unsupported_ocr_format and a failed row.
    unknown = json.loads(json.dumps(samples_by_engine["tesseract"]))
    unknown["ocr"]["engine"] = "google-vision"
    response = upload(client, json.dumps(unknown).encode(), "vision.json")
    assert response.status_code == 422
    assert response.json() == {"error": "unsupported_ocr_format"}
    row = db_conn.execute(
        "select status, error_code, ocr_engine from documents where upload_filename = 'vision.json'"
    ).fetchone()
    assert row == ("failed", "unsupported_ocr_format", "google-vision")

    # Re-uploading a rejected file gives the same error, and no second row.
    response = upload(client, junk, "scan_again.pdf")
    assert response.status_code == 422 and response.json() == {"error": "unreadable_file"}
    assert count(db_conn, "select count(*) from documents where upload_filename like 'scan%%'") == 1

    # Empty upload and no file part at all → 400 file_missing, no row.
    before = count(db_conn, "select count(*) from documents")
    response = upload(client, b"", "empty.json")
    assert response.status_code == 400 and response.json() == {"error": "file_missing"}
    response = client.post("/documents", data={"country_code": "SG"})
    assert response.status_code == 400 and response.json() == {"error": "file_missing"}
    assert count(db_conn, "select count(*) from documents") == before

    # Unknown id and malformed id → 404 not_found.
    for doc_id in (uuid4(), "not-a-uuid"):
        response = client.get(f"/documents/{doc_id}")
        assert response.status_code == 404 and response.json() == {"error": "not_found"}


def test_get_document_returns_stages_with_durations(db_conn, client, sample_bytes_by_engine):
    created = upload(client, variant(sample_bytes_by_engine["azure-document-intelligence"], "get"))
    assert created.status_code == 201
    doc_id = created.json()["document_id"]

    response = client.get(f"/documents/{doc_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == doc_id
    assert body["status"] == "embedded"
    assert body["document_type"] == "referral_letter"
    assert body["ocr_engine"] == "azure-document-intelligence"
    assert body["normalised_storage_uri"].endswith(f".v{NORMALISER_VERSION}.json")
    assert [s["stage"] for s in body["stages"]] == ["receive", "normalise", "load", "extract", "quality", "chunk", "embed"]
    for stage in body["stages"]:
        assert stage["status"] == "succeeded"
        assert isinstance(stage["duration_ms"], int) and stage["duration_ms"] >= 0
        assert stage["error"] is None
    assert {f["field_name"] for f in body["extracted_fields"]} == {
        "claimant_name", "provider_name", "signature_presence",
        "total_amount_paid", "total_approved_amount", "total_requested_amount",
    }
    assert all(f["validation_status"] == "valid" for f in body["extracted_fields"])
    assert all(c["passed"] for c in body["quality_checks"])
    assert {c["check_name"] for c in body["quality_checks"]} >= {
        "file_json", "envelope_valid", "engine_supported", "document_type_supported",
        "page_confidence", "bbox_clamped", "field_total_amount_paid",
    }
    [page] = body["pages"]
    assert page["page_number"] == 1 and page["line_count"] == 37
    assert page["size_unit"] == "inch"
    assert 0 < page["mean_confidence"] <= 1


@pytest.mark.parametrize(
    ("form_country", "envelope_country", "expected"),
    [("my", "SG", "MY"), (None, "PH", "PH"), (None, None, "SG")],
)
def test_country_code_resolution(db_conn, client, sample_bytes_by_engine, form_country, envelope_country, expected):
    data = json.loads(sample_bytes_by_engine["tesseract"])
    data["test_variant"] = f"country-{form_country}-{envelope_country}"
    if envelope_country is None:
        data["source"].pop("country_code")
    else:
        data["source"]["country_code"] = envelope_country
    form = {"country_code": form_country} if form_country else {}
    response = upload(client, json.dumps(data).encode(), **form)
    assert response.status_code == 201, response.text
    row = db_conn.execute("select country_code from documents where id = %s", (response.json()["document_id"],))
    assert row.fetchone()[0] == expected


def test_invalid_country_code_form_field(db_conn, client, sample_bytes_by_engine):
    response = upload(client, variant(sample_bytes_by_engine["tesseract"], "bad-country"), country_code="Singapore")
    assert response.status_code == 422 and response.json() == {"error": "invalid_country_code"}


def test_load_reads_normalised_zone_not_raw(db_conn, sample_bytes_by_engine):
    result = run_pipeline(variant(sample_bytes_by_engine["tesseract"], "reload"), "reload.json", conn=db_conn)
    assert result.status == "embedded"
    raw_uri, = db_conn.execute("select raw_storage_uri from documents where id = %s", (result.document_id,)).fetchone()
    storage.uri_to_path(raw_uri).unlink()

    run_id = start_run(db_conn, "renormalise")
    stage_load(db_conn, result.document_id, run_id)  # re-runnable on its own

    assert count(db_conn, "select count(*) from ocr_lines where document_id = %s", result.document_id) == 38
    assert count(db_conn, "select count(*) from ocr_lines where document_id = %s and run_id = %s",
                 result.document_id, run_id) == 38


def test_stage_failure_marks_document_failed(db_conn, sample_bytes_by_engine):
    result = run_pipeline(variant(sample_bytes_by_engine["tesseract"], "broken"), "broken.json", conn=db_conn)
    norm_uri, = db_conn.execute(
        "select normalised_storage_uri from documents where id = %s", (result.document_id,)
    ).fetchone()
    storage.uri_to_path(norm_uri).write_text("{ corrupted")

    run_id = start_run(db_conn, "renormalise")
    with pytest.raises(Exception):
        stage_load(db_conn, result.document_id, run_id)

    status, error_code = db_conn.execute(
        "select status, error_code from documents where id = %s", (result.document_id,)
    ).fetchone()
    assert (status, error_code) == ("failed", "internal_server_error")
    stage_status, error = db_conn.execute(
        "select status, error from document_stages where document_id = %s and run_id = %s and stage = 'load'",
        (result.document_id, run_id),
    ).fetchone()
    assert stage_status == "failed" and error
    # The failed load rolled back: the earlier pages/lines are still there.
    assert count(db_conn, "select count(*) from ocr_lines where document_id = %s", result.document_id) == 38


def test_cli_ingest_folder(db_conn, tmp_path, sample_bytes_by_engine, samples_by_engine):
    for engine, data in sample_bytes_by_engine.items():
        (tmp_path / f"{engine}.json").write_bytes(variant(data, "cli"))
    (tmp_path / "zz_copy.json").write_bytes(variant(sample_bytes_by_engine["tesseract"], "cli"))
    (tmp_path / "notes.txt").write_bytes(b"hello")
    unknown = dict(samples_by_engine["tesseract"], ocr={"engine": "abbyy"})
    (tmp_path / "abbyy.json").write_text(json.dumps(unknown))

    result = CliRunner().invoke(cli_app, ["ingest", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "processed: 3  duplicate: 1  failed: 2" in result.output
    assert "notes.txt: unreadable_file" in result.output
    assert "abbyy.json: unsupported_ocr_format" in result.output
    run_id = result.output.split("run ")[1].split()[0]
    status, stats = db_conn.execute("select status, stats from pipeline_runs where id = %s", (run_id,)).fetchone()
    assert status == "partial"
    assert stats == {"processed": 3, "duplicate": 1, "failed": 2}
    assert count(db_conn, "select count(*) from documents where latest_run_id = %s", run_id) == 5


@pytest.mark.parametrize("bad", ["Singapore", "S", "12", 65])
def test_invalid_envelope_country_code(db_conn, client, sample_bytes_by_engine, bad):
    """Same error as the form field; the file is still kept (raw zone + failed row)."""
    data = json.loads(sample_bytes_by_engine["tesseract"])
    data["source"]["country_code"] = bad
    payload = json.dumps(data).encode()

    response = upload(client, payload, f"bad-country-{bad}.json")

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_country_code"}
    status, error_code, raw_uri, country = db_conn.execute(
        "select status, error_code, raw_storage_uri, country_code from documents where upload_filename = %s",
        (f"bad-country-{bad}.json",),
    ).fetchone()
    assert (status, error_code, country) == ("failed", "invalid_country_code", "SG")
    assert storage.uri_to_path(raw_uri).read_bytes() == payload
    failed = db_conn.execute(
        "select check_name from quality_checks q join documents d on d.id = q.document_id"
        " where d.upload_filename = %s and not q.passed",
        (f"bad-country-{bad}.json",),
    ).fetchall()
    assert failed == [("envelope_valid",)]


def test_blank_envelope_country_code_falls_back_to_default(db_conn, client, sample_bytes_by_engine):
    data = json.loads(sample_bytes_by_engine["tesseract"])
    data["source"]["country_code"] = "  "
    data["test_variant"] = "blank-country"
    response = upload(client, json.dumps(data).encode())
    assert response.status_code == 201
    row = db_conn.execute("select country_code from documents where id = %s", (response.json()["document_id"],))
    assert row.fetchone()[0] == "SG"
