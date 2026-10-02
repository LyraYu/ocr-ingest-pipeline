import json
from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"

# Child tables first is not required with CASCADE, but keeps intent explicit.
DATA_TABLES = (
    "chunk_embeddings",
    "chunks",
    "quality_checks",
    "extracted_fields",
    "ocr_lines",
    "document_pages",
    "document_stages",
    "documents",
    "embedding_models",
    "pipeline_runs",
)


def _load_samples() -> dict[str, dict]:
    """Sample envelopes keyed by their declared ocr.engine (not by filename)."""
    samples: dict[str, dict] = {}
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        data = json.loads(path.read_bytes())
        samples[data["ocr"]["engine"]] = data
    return samples


@pytest.fixture(scope="session")
def samples_by_engine() -> dict[str, dict]:
    return _load_samples()


@pytest.fixture(scope="module")
def db_conn():
    """Connection to the compose Postgres with migrations applied and data tables
    truncated once per test module."""
    from app.db.connection import connect
    from app.db.migrate import migrate

    migrate()
    with connect(autocommit=True) as conn:
        conn.execute(f"truncate {', '.join(DATA_TABLES)} cascade")
        yield conn
