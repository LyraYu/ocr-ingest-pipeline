"""Pipeline stages (CLAUDE.md §5).

`stage_receive` creates the document, so it takes the uploaded bytes instead of
a document_id. Every later stage is `stage_x(conn, document_id, run_id) -> None`,
re-runnable on an existing document, and records its own document_stages row.

Each stage body runs in one transaction: on failure its partial writes roll back,
then the failed stage row and `documents.status = 'failed'` are written and
`StageFailed` is raised. The quality stage only flags: its own failure is recorded
on its stage row but never fails the document.

Each stage owns a set of quality_checks names and replaces only those on re-run.
"""

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import psycopg

from app.config import DEFAULT_COUNTRY_CODE, get_settings
from app.db import document_pages, document_stages, documents, extracted_fields, ocr_lines, quality_checks
from app.errors import PipelineInputError
from app.extraction import UnsupportedDocumentTypeError
from app.extraction.classify import classify
from app.extraction.rules import SourceLine, extract
from app.extraction.validate import validate_field
from app.ocr import NORMALISER_VERSION, OcrInputError
from app.ocr.envelope import Envelope, parse_json, validate_envelope
from app.ocr.registry import normalise_envelope
from app.ocr.schema import NormalisedDocument
from app.pipeline import storage

COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")


class StageFailed(Exception):
    def __init__(self, stage: str, error_code: str, message: str):
        super().__init__(f"{stage} failed ({error_code}): {message}")
        self.stage = stage
        self.error_code = error_code
        self.message = message


def _error_code(exc: BaseException) -> str:
    return exc.error_code if isinstance(exc, PipelineInputError) else "internal_server_error"


def _error_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _execute_stage(
    conn: psycopg.Connection,
    document_id: UUID,
    run_id: UUID,
    stage: str,
    success_status: str,
    body: Callable[[], None],
    on_failure: Callable[[Exception], None] | None = None,
    fail_document: bool = True,
) -> None:
    """`on_failure(exc)` runs inside the failure transaction (after the body's writes
    were rolled back), for rows that must record the failure itself."""
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    try:
        with conn.transaction():
            body()
            duration_ms = round((time.perf_counter() - t0) * 1000)
            document_stages.record(
                conn, document_id, stage, "succeeded", started_at, datetime.now(UTC), duration_ms, run_id
            )
            documents.set_status(conn, document_id, success_status, run_id)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000)
        code, message = _error_code(exc), _error_message(exc)
        with conn.transaction():
            document_stages.record(
                conn, document_id, stage, "failed", started_at, datetime.now(UTC), duration_ms,
                run_id, error=message,
            )
            if on_failure is not None:
                on_failure(exc)
            if fail_document:
                documents.mark_failed(conn, document_id, code, message, run_id)
        if fail_document:
            raise StageFailed(stage, code, message) from exc


# --- receive -----------------------------------------------------------------


def content_hash(file_bytes: bytes) -> tuple[str, str]:
    """(`sha256:<hex>`, `<hex>`)"""
    hex_digest = hashlib.sha256(file_bytes).hexdigest()
    return f"sha256:{hex_digest}", hex_digest


def normalise_country_code(value: str | None) -> str | None:
    """Upper-cased ISO-3166 alpha-2, None when blank; ValueError when malformed."""
    if value is None or not value.strip():
        return None
    code = value.strip().upper()
    if not COUNTRY_CODE_RE.match(code):
        raise ValueError(f"country_code must be two letters, got {value!r}")
    return code


RECEIVE_CHECKS = ("file_json", "envelope_valid", "engine_supported")


def receive_checks(error: OcrInputError | None) -> list[dict]:
    """The three receive-time checks in evaluation order. The failing one is
    passed=false; checks after it were not evaluated and are also passed=false,
    marked `evaluated: false`, so every document has the full set."""
    failed_at = RECEIVE_CHECKS.index(error.check) if error else len(RECEIVE_CHECKS)
    checks = []
    for i, name in enumerate(RECEIVE_CHECKS):
        if i < failed_at:
            details = {"evaluated": True}
        elif i == failed_at:
            details = {"evaluated": True, "error_code": error.error_code, "message": str(error)[:500]}
        else:
            details = {"evaluated": False, "message": f"not evaluated: {RECEIVE_CHECKS[failed_at]} failed"}
        checks.append({"check_name": name, "passed": i < failed_at, "severity": "error", "details": details})
    return checks


@dataclass(frozen=True)
class ReceiveOutcome:
    document_id: UUID
    duplicate: bool
    failed: bool


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _metadata_from_envelope(envelope: Envelope) -> dict:
    return {
        "source_sha256": envelope.source.source_sha256,
        "source_filename": envelope.source.original_filename,
        "source_mime_type": envelope.source.mime_type,
        "ocr_engine": envelope.ocr.engine,
        "ocr_engine_version": envelope.ocr.engine_version,
        "ocr_processed_at": envelope.ocr.processed_at,
    }


def _metadata_from_rejected_json(data: object) -> tuple[dict, str | None]:
    """Best-effort metadata from JSON that failed envelope validation, so the
    failed documents row still says e.g. which unknown engine was declared.
    Only plain strings are copied; nothing here is trusted further."""
    data = data if isinstance(data, dict) else {}
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    ocr = data.get("ocr") if isinstance(data.get("ocr"), dict) else {}
    try:
        country = normalise_country_code(_str_or_none(source.get("country_code")))
    except ValueError:
        country = None
    return {
        "source_sha256": _str_or_none(source.get("source_sha256")),
        "source_filename": _str_or_none(source.get("original_filename")),
        "source_mime_type": _str_or_none(source.get("mime_type")),
        "ocr_engine": _str_or_none(ocr.get("engine")),
        "ocr_engine_version": _str_or_none(ocr.get("engine_version")),
    }, country


def stage_receive(
    conn: psycopg.Connection,
    file_bytes: bytes,
    upload_filename: str | None,
    country_code: str | None,
    run_id: UUID,
) -> ReceiveOutcome:
    """Hash, duplicate check, raw-zone write, file/envelope/engine checks, documents row.

    `country_code` is the (already normalised) form field; resolution is
    form field → envelope `source.country_code` → DEFAULT_COUNTRY_CODE.
    A duplicate writes nothing and returns the existing document.
    """
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    hash_value, hex_digest = content_hash(file_bytes)

    existing = documents.get_by_content_hash(conn, hash_value)
    if existing:
        return ReceiveOutcome(existing["id"], duplicate=True, failed=existing["status"] == "failed")

    uri = storage.raw_uri(hex_digest)
    storage.write_bytes(uri, file_bytes)  # raw zone keeps every non-empty upload, valid or not

    error: OcrInputError | None = None
    metadata: dict = {}
    envelope_country: str | None = None
    try:
        data = parse_json(file_bytes)
        try:
            envelope = validate_envelope(data)
            metadata = _metadata_from_envelope(envelope)
            envelope_country = envelope.source.country_code
        except OcrInputError:
            metadata, envelope_country = _metadata_from_rejected_json(data)
            raise
    except OcrInputError as exc:
        error = exc

    with conn.transaction():
        document_id = documents.insert(
            conn,
            content_hash=hash_value,
            upload_filename=upload_filename,
            size_bytes=len(file_bytes),
            raw_storage_uri=uri,
            country_code=country_code or envelope_country or DEFAULT_COUNTRY_CODE,
            status="failed" if error else "received",
            error_code=error.error_code if error else None,
            error_message=_error_message(error) if error else None,
            latest_run_id=run_id,
            **metadata,
        )
        if document_id is None:  # lost a race with a concurrent upload of the same bytes
            existing = documents.get_by_content_hash(conn, hash_value)
            return ReceiveOutcome(existing["id"], duplicate=True, failed=existing["status"] == "failed")
        quality_checks.replace(conn, document_id, run_id, list(RECEIVE_CHECKS), receive_checks(error))
        document_stages.record(
            conn, document_id, "receive", "failed" if error else "succeeded", started_at,
            datetime.now(UTC), round((time.perf_counter() - t0) * 1000), run_id,
            error=_error_message(error) if error else None,
        )
    return ReceiveOutcome(document_id, duplicate=False, failed=error is not None)


# --- normalise ---------------------------------------------------------------


def stage_normalise(conn: psycopg.Connection, document_id: UUID, run_id: UUID) -> None:
    """Raw zone → normalised JSON at data/normalised/<hex>.v<version>.json."""

    def body() -> None:
        doc = documents.get(conn, document_id)
        envelope = validate_envelope(parse_json(storage.read_bytes(doc["raw_storage_uri"])))
        normalised = normalise_envelope(envelope)
        hex_digest = doc["content_hash"].removeprefix("sha256:")
        uri = storage.normalised_uri(hex_digest, NORMALISER_VERSION)
        storage.write_bytes(uri, normalised.model_dump_json(indent=2).encode())
        documents.set_normalised_uri(conn, document_id, uri)

    _execute_stage(conn, document_id, run_id, "normalise", "normalised", body)


# --- load --------------------------------------------------------------------


def stage_load(conn: psycopg.Connection, document_id: UUID, run_id: UUID) -> None:
    """Normalised JSON (never raw) → document_pages + ocr_lines. Replaces earlier pages."""

    def body() -> None:
        doc = documents.get(conn, document_id)
        if not doc["normalised_storage_uri"]:
            raise RuntimeError("document has no normalised_storage_uri; run normalise first")
        normalised = NormalisedDocument.model_validate_json(
            storage.read_bytes(doc["normalised_storage_uri"])
        )
        document_pages.delete_for_document(conn, document_id)
        for page in normalised.pages:
            page_id = document_pages.insert(
                conn,
                document_id=document_id,
                page_number=page.page_number,
                width=page.width,
                height=page.height,
                size_unit=page.size_unit,
                size_reason=page.size_reason,
                ocr_engine=normalised.engine.name,
                ocr_engine_version=normalised.engine.version,
                mean_confidence=page.mean_confidence,
                line_count=len(page.lines),
                run_id=run_id,
            )
            ocr_lines.insert_many(conn, document_id, page_id, run_id, page.lines)

    _execute_stage(conn, document_id, run_id, "load", "loaded", body)


# --- extract -----------------------------------------------------------------


def _pages_from_db(conn: psycopg.Connection, document_id: UUID) -> list[list[SourceLine]]:
    pages: dict[int, list[SourceLine]] = {}
    for row in ocr_lines.list_for_document(conn, document_id):
        pages.setdefault(row["page_number"], []).append(SourceLine(row["id"], row["text"]))
    return [pages[n] for n in sorted(pages)]


def stage_extract(conn: psycopg.Connection, document_id: UUID, run_id: UUID) -> None:
    """Classify, extract every §6.1 field of the type, validate; replaces earlier
    extracted_fields and document_type_supported rows. No type → document failed
    with unsupported_document_type and document_type_supported = false."""

    def body() -> None:
        pages = _pages_from_db(conn, document_id)
        classification = classify("\n".join(line.text for page in pages for line in page))
        rows = []
        for raw in extract(classification.document_type, pages):
            result = validate_field(raw.field_name, raw.value_type, raw.raw_value)
            rows.append({
                "field_name": raw.field_name,
                "raw_value": raw.raw_value,
                "normalised_value": result.normalised_value,
                "value_type": raw.value_type,
                "validation_status": result.status,
                "validation_message": result.message,
                "source_line_ids": raw.source_line_ids,
            })
        extracted_fields.replace_for_document(conn, document_id, run_id, rows)
        documents.set_document_type(conn, document_id, classification.document_type)
        quality_checks.replace(conn, document_id, run_id, ["document_type_supported"], [{
            "check_name": "document_type_supported",
            "passed": True,
            "severity": "error",
            "details": {
                "document_type": classification.document_type,
                "score": classification.score,
                "matched_keywords": list(classification.matched_keywords),
            },
        }])

    def on_failure(exc: Exception) -> None:
        if isinstance(exc, UnsupportedDocumentTypeError):
            extracted_fields.delete_for_document(conn, document_id)
            documents.set_document_type(conn, document_id, None)
            quality_checks.replace(conn, document_id, run_id, ["document_type_supported"], [{
                "check_name": "document_type_supported",
                "passed": False,
                "severity": "error",
                "details": {"error_code": exc.error_code, "message": str(exc)},
            }])

    _execute_stage(conn, document_id, run_id, "extract", "extracted", body, on_failure)


# --- quality -----------------------------------------------------------------

QUALITY_OWNED_CHECKS = ["page_confidence", "bbox_clamped", r"field\_%"]


def quality_checks_for(pages: list[dict], fields: list[dict], threshold: float) -> list[dict]:
    """Pure: page_confidence per page + document level, bbox_clamped per page,
    field_<name> per extracted field. `pages`: page_id, page_number,
    mean_confidence, bbox_clamped_count. `fields`: extracted_fields rows."""
    checks = []
    flagged_pages = []
    for page in pages:
        conf = page["mean_confidence"]
        passed = conf is not None and conf >= threshold
        if not passed:
            flagged_pages.append(page["page_number"])
        checks.append({
            "check_name": "page_confidence", "page_id": page["page_id"], "passed": passed,
            "severity": "warning",
            "details": {"threshold": threshold, "observed": conf, "page_number": page["page_number"],
                        **({} if conf is not None else {"message": "no line confidence on page"})},
        })
        checks.append({
            "check_name": "bbox_clamped", "page_id": page["page_id"],
            "passed": page["bbox_clamped_count"] == 0, "severity": "warning",
            "details": {"clamped_count": page["bbox_clamped_count"], "page_number": page["page_number"]},
        })
    checks.append({
        "check_name": "page_confidence", "page_id": None, "passed": not flagged_pages,
        "severity": "warning",
        "details": {"threshold": threshold, "flagged_pages": flagged_pages, "level": "document"},
    })
    for f in fields:
        checks.append({
            "check_name": f"field_{f['field_name']}", "passed": f["validation_status"] == "valid",
            "severity": "warning",
            "details": {"validation_status": f["validation_status"], "message": f["validation_message"]},
        })
    return checks


def stage_quality(conn: psycopg.Connection, document_id: UUID, run_id: UUID) -> None:
    """Flags only: writes quality_checks, never fails the document."""

    def body() -> None:
        doc = documents.get(conn, document_id)
        normalised = NormalisedDocument.model_validate_json(storage.read_bytes(doc["normalised_storage_uri"]))
        clamped = {p.page_number: p.bbox_clamped_count for p in normalised.pages}
        pages = [
            {"page_id": p["id"], "page_number": p["page_number"],
             "mean_confidence": None if p["mean_confidence"] is None else float(p["mean_confidence"]),
             "bbox_clamped_count": clamped.get(p["page_number"], 0)}
            for p in document_pages.list_for_document(conn, document_id)
        ]
        checks = quality_checks_for(
            pages, extracted_fields.list_for_document(conn, document_id),
            get_settings().quality_min_page_confidence,
        )
        quality_checks.replace(conn, document_id, run_id, QUALITY_OWNED_CHECKS, checks)

    _execute_stage(conn, document_id, run_id, "quality", "extracted", body, fail_document=False)


# Stages after receive, in order. chunk / embed arrive in phase 4.
POST_RECEIVE_STAGES: tuple[Callable[[psycopg.Connection, UUID, UUID], None], ...] = (
    stage_normalise,
    stage_load,
    stage_extract,
    stage_quality,
)
