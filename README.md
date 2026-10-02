# Document Ingestion & Embedding Pipeline

OCR engine exports (AWS Textract, Tesseract, Azure Document Intelligence) →
normalised OCR JSON → PostgreSQL → chunks + pgvector embeddings → search API.
The design spec is `CLAUDE.md`.

> Status: phase 1 (scaffold, schema, normalisers). Ingest, extraction, embeddings
> and search arrive in later phases.

## Run with Docker

```bash
docker compose up -d db                          # Postgres 16 + pgvector on :5432 (docs/docs/docs)
docker compose build api
docker compose run --rm api python -m app.cli migrate   # apply migrations/*.sql
docker compose up -d api                         # runs migrate, then uvicorn on :8000
curl localhost:8000/health
```

## Tests

Tests run against the compose Postgres (each DB test module truncates the data tables).

```bash
docker compose up -d db
docker compose build api                         # rebuild after code changes
docker compose run --rm api pytest -v
```

Running locally instead (Python 3.12):

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && set -a && . ./.env && set +a
python -m app.cli migrate
pytest -v
```

## Migrations

Numbered SQL files in `migrations/` are applied in filename order by
`python -m app.cli migrate`; each applied file is recorded in
`schema_migrations(filename, applied_at)`. Each file runs in one transaction.

## Normalisation

`app/ocr/` turns each engine's `raw_output` into one schema
(`app/ocr/schema.py`, `NORMALISER_VERSION = "1.0.0"`): bboxes `[x0, y0, x1, y1]`
as fractions of the page (origin top-left), confidences in [0, 1].

| engine | page size | line confidence |
|---|---|---|
| aws-textract | null (`size_reason` explains: ratio coordinates only) | LINE `Confidence / 100` |
| tesseract | level-1 row, px | mean of word `conf` (ignoring -1) / 100 |
| azure-document-intelligence | page `width`/`height`, inch (or px) | mean of words whose span lies in the line's span |

Errors: non-JSON → `unreadable_file`; missing envelope keys, unknown engine, or a
raw_output that does not match the declared engine → `unsupported_ocr_format`.

## Known limitations

- `chunk_embeddings.embedding` is `vector(384)`; a model with another dimension
  needs a new migration. Both configured models are 384-dimensional.
