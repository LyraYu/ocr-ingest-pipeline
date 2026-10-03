"""run_pipeline: one uploaded file through every registered stage. Never raises."""

import logging
from dataclasses import dataclass, field
from uuid import UUID

import psycopg

from app.config import get_settings
from app.chunking import CHUNKING_VERSION
from app.db import chunks, document_pages, document_stages, documents, embedding_models, pipeline_runs, quality_checks
from app.db.connection import connect
from app.ocr import NORMALISER_VERSION
from app.pipeline.stages import POST_RECEIVE_STAGES, StageFailed, content_hash, ingest_model_name, stage_receive

log = logging.getLogger(__name__)

RUN_TYPES = ("ingest", "reembed", "renormalise")


@dataclass
class IngestResult:
    document_id: UUID | None = None
    content_hash: str | None = None
    duplicate: bool = False
    document_type: str | None = None
    status: str | None = None
    error_code: str | None = None
    pages: int = 0
    chunks: int = 0
    quality_flags: list[str] = field(default_factory=list)
    timings_ms: dict[str, int | None] = field(
        default_factory=lambda: {"normalise": None, "load": None, "embed": None}
    )

    @property
    def outcome(self) -> str:
        """processed / duplicate / failed — the CLI and run stats buckets."""
        if self.error_code:
            return "failed"
        return "duplicate" if self.duplicate else "processed"

    def response_body(self) -> dict:
        """The POST /documents success body (CLAUDE.md §9)."""
        return {
            "document_id": str(self.document_id),
            "content_hash": self.content_hash,
            "duplicate": self.duplicate,
            "document_type": self.document_type,
            "status": self.status,
            "pages": self.pages,
            "chunks": self.chunks,
            "quality_flags": self.quality_flags,
            "timings_ms": self.timings_ms,
        }


def start_run(conn: psycopg.Connection, run_type: str, embedding_model: str | None = None) -> UUID:
    """`embedding_model` defaults to the model ingest would embed with now."""
    if run_type not in RUN_TYPES:
        raise ValueError(f"run_type must be one of {RUN_TYPES}, got {run_type!r}")
    model = embedding_model or ingest_model_name(conn)
    active = embedding_models.get_active(conn)
    version = active["model_version"] if active and active["model_name"] == model else None
    return pipeline_runs.create(
        conn, run_type, NORMALISER_VERSION, get_settings().code_version, model, version
    )


def run_status(stats: dict[str, int]) -> str:
    """succeeded: nothing failed; failed: everything attempted failed; else partial."""
    if not stats.get("failed"):
        return "succeeded"
    return "failed" if stats["failed"] == sum(stats.values()) else "partial"


def finish_run(conn: psycopg.Connection, run_id: UUID, stats: dict[str, int]) -> None:
    pipeline_runs.finish(conn, run_id, run_status(stats), stats)


def describe_document(conn: psycopg.Connection, document_id: UUID, duplicate: bool) -> IngestResult:
    doc = documents.get(conn, document_id)
    durations = document_stages.latest_durations(conn, document_id)
    return IngestResult(
        document_id=doc["id"],
        content_hash=doc["content_hash"],
        duplicate=duplicate,
        document_type=doc["document_type"],
        status=doc["status"],
        error_code=doc["error_code"],
        pages=document_pages.count_for_document(conn, document_id),
        chunks=chunks.count_for_document(conn, document_id, CHUNKING_VERSION),
        quality_flags=quality_checks.failed_check_names(conn, document_id),
        timings_ms={s: durations.get(s) for s in ("normalise", "load", "embed")},
    )


def _run(
    conn: psycopg.Connection,
    file_bytes: bytes | None,
    filename: str | None,
    country_code: str | None,
    run_type: str,
    run_id: UUID | None,
    stages,
) -> IngestResult:
    if not file_bytes:
        return IngestResult(error_code="file_missing")

    # Duplicates write nothing, not even a pipeline_runs row of their own.
    hash_value, _ = content_hash(file_bytes)
    existing = documents.get_by_content_hash(conn, hash_value)
    if existing:
        return describe_document(conn, existing["id"], duplicate=True)

    own_run = run_id is None
    if own_run:
        run_id = start_run(conn, run_type)
    document_id = None
    try:
        received = stage_receive(conn, file_bytes, filename, country_code, run_id)
        document_id = received.document_id
        if not received.duplicate and not received.failed:
            for stage in stages:
                try:
                    stage(conn, document_id, run_id)
                except StageFailed as exc:
                    log.warning("document %s: stage %s failed (%s)", document_id, exc.stage, exc.error_code)
                    break
        result = describe_document(conn, document_id, duplicate=received.duplicate)
    except Exception:
        # Anything outside a stage's own handling (e.g. raw-zone write, DB error).
        log.exception("document %s: unexpected pipeline error", document_id)
        result = IngestResult(document_id=document_id, content_hash=hash_value,
                              status="failed", error_code="internal_server_error")
        if document_id is not None:
            _mark_failed_quietly(conn, document_id, run_id)
    if own_run:
        try:
            finish_run(conn, run_id, {"processed": 0, "duplicate": 0, "failed": 0} | {result.outcome: 1})
        except Exception:
            log.exception("run %s: could not record run outcome", run_id)
    return result


def _mark_failed_quietly(conn: psycopg.Connection, document_id: UUID, run_id: UUID) -> None:
    try:
        documents.mark_failed(conn, document_id, "internal_server_error", "unexpected pipeline error", run_id)
    except Exception:
        log.exception("document %s: could not mark failed", document_id)


def run_pipeline(
    file_bytes: bytes | None,
    filename: str | None,
    country_code: str | None = None,
    run_type: str = "ingest",
    *,
    conn: psycopg.Connection | None = None,
    run_id: UUID | None = None,
    stages=POST_RECEIVE_STAGES,
) -> IngestResult:
    """Run one file through receive and the registered stages.

    `country_code` must already be normalised (see stages.normalise_country_code).
    Pass `conn` (autocommit) and `run_id` to process a batch under one
    pipeline_runs row; without `run_id` a run is created and finished here.
    `stages` defaults to every stage; the CLI passes PRE_EMBED_STAGES and embeds the
    whole batch at the end in one encode call.
    Never raises: failures come back as `IngestResult.error_code`.
    """
    try:
        if conn is not None:
            return _run(conn, file_bytes, filename, country_code, run_type, run_id, stages)
        with connect(autocommit=True) as own_conn:
            return _run(own_conn, file_bytes, filename, country_code, run_type, run_id, stages)
    except Exception:
        log.exception("pipeline error before a document row existed")
        return IngestResult(status="failed", error_code="internal_server_error")
