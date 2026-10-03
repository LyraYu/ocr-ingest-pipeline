"""Phase 3: classification, field rules, extract/quality stages, v_quarantine."""

import copy
import json
from pathlib import PurePath

import pytest

from app.extraction import UnsupportedDocumentTypeError
from app.extraction.classify import classify
from app.extraction.rules import SourceLine, extract
from app.pipeline import storage
from app.pipeline.runner import run_pipeline, start_run
from app.pipeline.stages import stage_extract

# --- pure: classify and rules ---------------------------------------------------


def test_classify_weights_titles_over_incidental_keywords():
    letterhead = "CLINIC PTE LTD\nGST Reg No: M9-1\nMEDICAL CERTIFICATE\nunfit for duty"
    assert classify(letterhead).document_type == "medical_certificate"
    assert classify("Tax Invoice\nItem: medical certificate fee").document_type == "receipt"
    assert classify("REFERRAL\nLETTER").document_type == "referral_letter"  # phrase across lines


def test_classify_without_keywords_raises():
    with pytest.raises(UnsupportedDocumentTypeError):
        classify("Lorem ipsum\nGSTR is not a keyword")


def _lines(*texts):
    return [[SourceLine(i, t) for i, t in enumerate(texts)]]


def _field(fields, name):
    return next(f for f in fields if f.field_name == name)


def test_label_value_on_next_line_and_prose_not_a_label():
    fields = extract("medical_certificate", _lines(
        "Some Clinic", "the above-named patient is unfit", "Patient Name:", "JANE DOE",
    ))
    name = _field(fields, "claimant_name")
    assert name.raw_value == "JANE DOE"
    assert name.source_line_ids == [2, 3]


def test_empty_label_does_not_take_the_next_label_as_value():
    fields = extract("receipt", _lines("Clinic", "Patient:", "NRIC/FIN:", "S0000000A"))
    assert _field(fields, "claimant_name").raw_value is None


def test_default_provider_skips_fullerton_health_lines():
    fields = extract("receipt", _lines("Panel clinic of Fullerton Health network", "REAL CLINIC PTE LTD"))
    provider = _field(fields, "provider_name")
    assert provider.raw_value == "REAL CLINIC PTE LTD" and provider.source_line_ids == [1]


def test_every_field_of_the_type_is_returned():
    for doc_type in ("referral_letter", "medical_certificate", "receipt"):
        fields = extract(doc_type, _lines("nothing useful here"))
        assert len({f.field_name for f in fields}) == len(fields) > 0


# --- pipeline: samples ------------------------------------------------------------


def expected_type(sample: dict) -> str:
    """The samples' scan filenames name their type (receipt.pdf → receipt)."""
    return PurePath(sample["source"]["original_filename"]).stem


def fields_of(conn, document_id) -> dict[str, tuple]:
    rows = conn.execute(
        "select field_name, validation_status, normalised_value, raw_value from extracted_fields"
        " where document_id = %s",
        (document_id,),
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


@pytest.mark.parametrize("engine", ["aws-textract", "tesseract", "azure-document-intelligence"])
def test_sample_extraction(db_conn, sample_bytes_by_engine, samples_by_engine, engine):
    result = run_pipeline(sample_bytes_by_engine[engine], f"{engine}.json", conn=db_conn)

    assert result.error_code is None
    assert result.status == "extracted"
    assert result.document_type == expected_type(samples_by_engine[engine])
    fields = fields_of(db_conn, result.document_id)
    assert fields["claimant_name"][0] == "valid"
    status, provider, _ = fields["provider_name"]
    assert status == "valid" and "fullerton health" not in provider.lower()
    dated_or_amount = [
        name for name in fields
        if (name.endswith("amount") or name.startswith("total_amount") or "date" in name)
        and fields[name][0] == "valid"
    ]
    assert dated_or_amount, fields
    # Every field of the type has a row; the samples are clean, so nothing is flagged.
    assert all(status == "valid" for status, _, _ in fields.values()), fields
    assert result.quality_flags == []


def test_amounts_are_stored_in_smallest_unit(db_conn, sample_bytes_by_engine):
    data = json.loads(sample_bytes_by_engine["azure-document-intelligence"])
    data["test_variant"] = "amounts"
    result = run_pipeline(json.dumps(data).encode(), "amounts.json", conn=db_conn)
    fields = fields_of(db_conn, result.document_id)
    assert fields["total_requested_amount"][1:] == (120000, "SGD 1,200.00")


def test_extract_is_rerunnable(db_conn, sample_bytes_by_engine):
    data = json.loads(sample_bytes_by_engine["tesseract"])
    data["test_variant"] = "rerun"
    result = run_pipeline(json.dumps(data).encode(), "rerun.json", conn=db_conn)
    before = fields_of(db_conn, result.document_id)

    run_id = start_run(db_conn, "renormalise")
    stage_extract(db_conn, result.document_id, run_id)

    assert fields_of(db_conn, result.document_id) == before
    runs = db_conn.execute(
        "select distinct run_id from extracted_fields where document_id = %s", (result.document_id,)
    ).fetchall()
    assert runs == [(run_id,)]


# --- unsupported type, receive checks, quality, quarantine ---------------------------------


def _without_keywords(sample: dict) -> dict:
    data = copy.deepcopy(sample)
    raw = data["raw_output"]
    raw["text"] = [f"word{i}" if t.strip() else t for i, t in enumerate(raw["text"])]
    data["test_variant"] = "no-type"
    return data


def test_unsupported_document_type(db_conn, client, samples_by_engine):
    data = json.dumps(_without_keywords(samples_by_engine["tesseract"])).encode()
    response = client.post("/documents", files={"file": ("mystery.json", data, "application/json")})

    assert response.status_code == 422
    assert response.json() == {"error": "unsupported_document_type"}
    doc_id, status, error_code, doc_type, raw_uri = db_conn.execute(
        "select id, status, error_code, document_type, raw_storage_uri from documents"
        " where upload_filename = 'mystery.json'"
    ).fetchone()
    assert (status, error_code, doc_type) == ("failed", "unsupported_document_type", None)
    assert storage.uri_to_path(raw_uri).read_bytes() == data
    checks = dict(db_conn.execute(
        "select check_name, passed from quality_checks where document_id = %s", (doc_id,)
    ).fetchall())
    assert checks == {
        "file_json": True, "envelope_valid": True, "engine_supported": True,
        "document_type_supported": False,
    }
    assert db_conn.execute(
        "select count(*) from extracted_fields where document_id = %s", (doc_id,)
    ).fetchone()[0] == 0

    row = db_conn.execute(
        "select status, error_code, failed_checks from v_quarantine where document_id = %s", (doc_id,)
    ).fetchone()
    assert row == ("failed", "unsupported_document_type", ["document_type_supported"])


def test_receive_checks_for_rejected_files(db_conn, samples_by_engine):
    unknown = copy.deepcopy(samples_by_engine["tesseract"])
    unknown["ocr"]["engine"] = "google-vision"
    cases = {
        b"not json at all": ("unreadable_file", {"file_json": False, "envelope_valid": False, "engine_supported": False}),
        b'{"hello": "world"}': ("unsupported_ocr_format", {"file_json": True, "envelope_valid": False, "engine_supported": False}),
        json.dumps(unknown).encode(): ("unsupported_ocr_format", {"file_json": True, "envelope_valid": True, "engine_supported": False}),
    }
    for payload, (code, expected) in cases.items():
        result = run_pipeline(payload, "rejected.json", conn=db_conn)
        assert result.error_code == code
        rows = db_conn.execute(
            "select check_name, passed, details from quality_checks where document_id = %s", (result.document_id,)
        ).fetchall()
        assert {name: passed for name, passed, _ in rows} == expected
        failed = [details for _, passed, details in rows if not passed]
        assert sum(d["evaluated"] for d in failed) == 1  # exactly one check actually failed
        quarantined = db_conn.execute(
            "select error_code from v_quarantine where document_id = %s", (result.document_id,)
        ).fetchone()
        assert quarantined == (code,)


def test_low_confidence_and_clamped_boxes_are_flagged_not_failed(db_conn, client, samples_by_engine):
    data = copy.deepcopy(samples_by_engine["aws-textract"])
    data["test_variant"] = "low-quality"
    lines = [b for b in data["raw_output"]["Blocks"] if b["BlockType"] == "LINE"]
    for block in lines:
        block["Confidence"] = 55.0
    lines[0]["Geometry"]["BoundingBox"]["Left"] = -0.03  # off-page → clamped

    response = client.post("/documents", files={"file": ("low.json", json.dumps(data).encode(), "application/json")})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "extracted"
    assert body["quality_flags"] == ["bbox_clamped", "page_confidence"]
    doc_id = body["document_id"]
    page_checks = {
        (name, page): (passed, details)
        for name, page, passed, details in db_conn.execute(
            "select check_name, page_id is not null, passed, details from quality_checks"
            " where document_id = %s and check_name in ('page_confidence', 'bbox_clamped')",
            (doc_id,),
        ).fetchall()
    }
    passed, details = page_checks[("page_confidence", True)]
    assert not passed and details["observed"] == pytest.approx(0.55) and details["threshold"] == 0.80
    assert page_checks[("page_confidence", False)][0] is False  # document-level warning
    assert page_checks[("bbox_clamped", True)] == (False, {"clamped_count": 1, "page_number": 1})

    quarantined = db_conn.execute(
        "select status, error_code, failed_checks from v_quarantine where document_id = %s", (doc_id,)
    ).fetchone()
    assert quarantined == ("extracted", None, ["bbox_clamped", "page_confidence"])


def test_clean_documents_are_not_quarantined(db_conn):
    clean = db_conn.execute(
        "select count(*) from documents d where d.status <> 'failed'"
        " and not exists (select 1 from quality_checks q where q.document_id = d.id and not q.passed)"
    ).fetchone()[0]
    assert clean > 0
    leaked = db_conn.execute(
        "select count(*) from v_quarantine v join documents d on d.id = v.document_id"
        " where d.status <> 'failed'"
        " and not exists (select 1 from quality_checks q where q.document_id = d.id and not q.passed)"
    ).fetchone()[0]
    assert leaked == 0
