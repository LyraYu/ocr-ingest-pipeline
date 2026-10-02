"""Pipeline stages (CLAUDE.md §5).

`stage_receive` creates the document, so it takes the uploaded bytes instead of
a document_id. Every later stage is `stage_x(conn, document_id, run_id) -> None`,
re-runnable on an existing document, and records its own document_stages row.

Each stage body runs in one transaction: on failure its partial writes roll back,
then the failed stage row and `documents.status = 'failed'` are written and
`StageFailed` is raised.
"""

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import psycopg

from app.config import DEFAULT_COUNTRY_CODE
from app.db import document_pages, document_stages, documents, ocr_lines
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
    return exc.error_code if isinstance(exc, OcrInputError) else "internal_server_error"


def _error_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _execute_stage(
    conn: psycopg.Connection,
    document_id: UUID,
    run_id: UUID,
    stage: str,
    success_status: str,
    body: Callable[[], None],
) -> None:
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
            documents.mark_failed(conn, document_id, code, message, run_id)
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


# Stages after receive, in order. extract / quality / chunk / embed arrive in phases 3–4.
POST_RECEIVE_STAGES: tuple[Callable[[psycopg.Connection, UUID, UUID], None], ...] = (
    stage_normalise,
    stage_load,
)
