import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
TEST_DB_NAME = "docs_test"


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def pytest_configure(config):
    """Point the whole test session at the `docs_test` database, never at the
    database DATABASE_URL names (`docs`, the one the API and CLI use). The database
    is created if missing. TEST_DATABASE_URL overrides the derived URL."""
    base_url = os.environ.get("DATABASE_URL")
    if not base_url:
        return  # pure unit tests can still run; DB tests fail on the missing setting
    test_url = os.environ.get("TEST_DATABASE_URL") or _with_database(base_url, TEST_DB_NAME)
    test_db = urlsplit(test_url).path.lstrip("/")
    if test_db == urlsplit(base_url).path.lstrip("/") and not os.environ.get("TEST_DATABASE_URL"):
        raise pytest.UsageError(f"refusing to run tests against the application database {test_db!r}")

    import psycopg
    from psycopg import sql

    with psycopg.connect(_with_database(test_url, "postgres"), autocommit=True) as admin:
        exists = admin.execute("select 1 from pg_database where datname = %s", (test_db,)).fetchone()
        if not exists:
            admin.execute(sql.SQL("create database {}").format(sql.Identifier(test_db)))

    os.environ["DATABASE_URL"] = test_url
    from app.config import get_settings

    get_settings.cache_clear()

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


def _sample_bytes() -> dict[str, bytes]:
    """Sample file bytes keyed by their declared ocr.engine (not by filename)."""
    return {
        json.loads(path.read_bytes())["ocr"]["engine"]: path.read_bytes()
        for path in sorted(SAMPLES_DIR.glob("*.json"))
    }


@pytest.fixture(scope="session")
def sample_bytes_by_engine() -> dict[str, bytes]:
    return _sample_bytes()


@pytest.fixture(scope="session")
def samples_by_engine(sample_bytes_by_engine) -> dict[str, dict]:
    return {engine: json.loads(data) for engine, data in sample_bytes_by_engine.items()}


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir(tmp_path_factory):
    """Raw/normalised zones go to a temp dir, never the real ./data."""
    from app.config import get_settings

    data_dir = tmp_path_factory.mktemp("data")
    previous = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(data_dir)
    get_settings.cache_clear()
    yield data_dir
    if previous is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = previous
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


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
