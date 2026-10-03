"""Phase 4: chunk + embed stages, POST /search, reembed."""

import pytest
from typer.testing import CliRunner

from app.cli import app as cli_app

SECOND_MODEL = "BAAI/bge-small-en-v1.5"


def upload(client, data: bytes, filename: str):
    return client.post("/documents", files={"file": (filename, data, "application/json")})


@pytest.fixture(scope="module")
def ingested(db_conn, client, sample_bytes_by_engine):
    """The three samples, ingested once for the module: engine → POST response body."""
    bodies = {}
    for engine, data in sample_bytes_by_engine.items():
        response = upload(client, data, f"{engine}.json")
        assert response.status_code == 201, response.text
        bodies[engine] = response.json()
    return bodies


def search(client, **body):
    response = client.post("/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def test_ingest_embeds_every_chunk(db_conn, ingested):
    for body in ingested.values():
        assert body["status"] == "embedded"
        assert body["chunks"] > 0
        assert body["timings_ms"]["embed"] >= 0
    missing = db_conn.execute(
        "select count(*) from chunks c where not exists (select 1 from chunk_embeddings e where e.chunk_id = c.id)"
    ).fetchone()[0]
    assert missing == 0
    model, active = db_conn.execute("select model_name, is_active from embedding_models").fetchone()
    assert (model, active) == ("sentence-transformers/all-MiniLM-L6-v2", True)


def test_end_to_end(client, ingested):
    mc_id = ingested["aws-textract"]["document_id"]
    assert ingested["aws-textract"]["document_type"] == "medical_certificate"

    results = search(
        client, query="medical leave for acute gastroenteritis", top_k=3,
        filters={"document_type": "medical_certificate"},
    )

    assert results
    top = results[0]
    assert top["document_id"] == mc_id
    assert top["document_type"] == "medical_certificate"
    assert top["page"] == 1
    assert top["embedding_model"] == "sentence-transformers/all-MiniLM-L6-v2"
    assert len(top["bbox"]) == 4 and all(0.0 <= v <= 1.0 for v in top["bbox"])
    assert top["bbox"][0] <= top["bbox"][2] and top["bbox"][1] <= top["bbox"][3]
    assert -1.0 <= top["score"] <= 1.0
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)
    assert all(r["document_type"] == "medical_certificate" for r in results)


def test_unfiltered_search_ranks_the_relevant_document_first(client, ingested):
    [top] = search(client, query="total amount paid for the tax invoice", top_k=1)
    assert top["document_type"] == "receipt"


def test_filter_that_matches_nothing_returns_empty(client, ingested):
    assert search(client, query="medical leave", filters={"country_code": "VN"}) == []
    assert search(client, query="medical leave", filters={"country_code": "sg"})  # case-insensitive


def test_top_k_limits_results(client, ingested):
    assert len(search(client, query="clinic", top_k=2)) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"query": "x", "top_k": 0},
        {"query": "x", "top_k": 51},
        {"query": "x", "top_k": "five"},
        {"query": "x", "top_k": 2.5},
        {"query": "   "},
        {"top_k": 5},
        {"query": "x", "filters": {"document_type": "invoice"}},
        {"query": "x", "filters": {"country_code": "SGP"}},
        {"query": "x", "unknown": 1},
    ],
)
def test_search_validation(client, body):
    response = client.post("/search", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"
    assert response.json()["detail"]


def test_malformed_json_body_uses_error_shape(client):
    response = client.post("/search", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 422 and response.json()["error"] == "invalid_request"


def test_reembed(db_conn, client, ingested):
    def counts():
        return dict(db_conn.execute(
            "select model_name, count(*) from chunk_embeddings group by model_name"
        ).fetchall())

    chunks_before = db_conn.execute("select count(*) from chunks").fetchone()[0]
    old = counts()

    result = CliRunner().invoke(cli_app, ["reembed", "--model", SECOND_MODEL])

    assert result.exit_code == 0, result.output
    assert f"active model: {SECOND_MODEL}" in result.output
    assert db_conn.execute("select count(*) from chunks").fetchone()[0] == chunks_before
    new = counts()
    assert new[SECOND_MODEL] == chunks_before
    assert new["sentence-transformers/all-MiniLM-L6-v2"] == old["sentence-transformers/all-MiniLM-L6-v2"]
    per_chunk = db_conn.execute(
        "select min(n), max(n) from (select count(*) as n from chunk_embeddings group by chunk_id) t"
    ).fetchone()
    assert per_chunk == (2, 2)
    active = db_conn.execute("select model_name from embedding_models where is_active").fetchall()
    assert active == [(SECOND_MODEL,)]
    run_type, status, model = db_conn.execute(
        "select run_type, status, embedding_model from pipeline_runs where run_type = 'reembed'"
    ).fetchone()
    assert (run_type, status, model) == ("reembed", "succeeded", SECOND_MODEL)
    # Status is unchanged by the re-run of chunk/embed.
    assert db_conn.execute("select count(*) from documents where status <> 'embedded'").fetchone()[0] == 0

    results = search(
        client, query="medical leave for acute gastroenteritis",
        filters={"document_type": "medical_certificate"},
    )
    assert results and all(r["embedding_model"] == SECOND_MODEL for r in results)
    assert results[0]["document_id"] == ingested["aws-textract"]["document_id"]

    # New uploads are embedded with the active model only.
    data = b'{"x": 1}'  # rejected upload: no embeddings appear for it
    upload(client, data, "noise.json")
    assert counts() == new
