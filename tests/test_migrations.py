from app.db.migrate import MIGRATIONS_DIR, migrate

EXPECTED_TABLES = {
    "schema_migrations", "pipeline_runs", "documents", "document_stages", "document_pages",
    "ocr_lines", "extracted_fields", "quality_checks", "chunks", "chunk_embeddings",
    "embedding_models",
}
EXPECTED_VIEWS = {"v_quarantine", "v_documents_deidentified"}


def test_schema_applied_and_migrate_is_idempotent(db_conn):
    assert migrate() == []  # db_conn already applied everything
    rows = db_conn.execute(
        "select table_name, table_type from information_schema.tables where table_schema = 'public'"
    ).fetchall()
    tables = {name for name, kind in rows if kind == "BASE TABLE"}
    views = {name for name, kind in rows if kind == "VIEW"}
    assert EXPECTED_TABLES <= tables
    assert EXPECTED_VIEWS <= views
    applied = [r[0] for r in db_conn.execute("select filename from schema_migrations order by filename")]
    assert applied == sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert applied[:2] == ["001_initial.sql", "002_info_severity_and_country_error.sql"]
    assert db_conn.execute("select 1 from pg_extension where extname = 'vector'").fetchone()
    assert db_conn.execute(
        "select 1 from pg_indexes where indexname = 'chunk_embeddings_embedding_hnsw_idx'"
        " and indexdef ilike '%hnsw%vector_cosine_ops%'"
    ).fetchone()


def test_tests_use_the_test_database(db_conn):
    assert db_conn.execute("select current_database()").fetchone() == ("docs_test",)


def test_runs_record_the_code_version(db_conn):
    from app.config import get_settings
    from app.pipeline.runner import start_run

    run_id = start_run(db_conn, "ingest")
    recorded = db_conn.execute("select code_version from pipeline_runs where id = %s", (run_id,)).fetchone()[0]
    assert recorded == get_settings().code_version
