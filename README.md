# Document Ingestion & Embedding Pipeline

OCR engine exports (AWS Textract, Tesseract, Azure Document Intelligence) →
normalised OCR JSON → PostgreSQL → chunks + pgvector embeddings → search API.
The design spec is `CLAUDE.md`.

> Status: phase 2 (receive → normalise → load, API, CLI ingest). Extraction,
> quality checks, chunking, embeddings and search arrive in later phases, so a
> document currently finishes with `status = loaded`, `document_type = null`, `chunks = 0`.

## Run with Docker

```bash
docker compose up -d db                          # Postgres 16 + pgvector on :5432 (docs/docs/docs)
docker compose build api
docker compose run --rm api python -m app.cli migrate   # apply migrations/*.sql
docker compose up -d api                         # runs migrate, then uvicorn on :8000
curl localhost:8000/health
```

## API

```bash
# Upload an export (country_code optional: form field → envelope source.country_code → SG)
curl -s -F file=@samples/receipt.json -F country_code=SG localhost:8000/documents
# → 201 {"document_id", "content_hash", "duplicate": false, "document_type", "status",
#        "pages", "chunks", "quality_flags", "timings_ms": {"normalise", "load", "embed"}}

# Same bytes again (any filename) → 200 with the existing document and "duplicate": true
curl -s -F file=@samples/receipt.json localhost:8000/documents

# Document metadata, per-stage status/durations, quality checks, extracted fields, pages
curl -s localhost:8000/documents/<document_id>
```

Errors use the body `{"error": "<code>"}`:

| HTTP | code | when |
|---|---|---|
| 400 | `file_missing` | no `file` part, or an empty file |
| 422 | `unreadable_file` | not JSON (still stored in the raw zone, `documents` row with `status = failed`) |
| 422 | `unsupported_ocr_format` | no envelope, unknown `ocr.engine`, or `raw_output` not in that engine's shape (stored + failed row) |
| 422 | `unsupported_document_type` | classification found no supported type (phase 3) |
| 422 | `invalid_country_code` | `country_code` form field is not two letters |
| 404 | `not_found` | `GET /documents/{id}` for an unknown or malformed id |
| 500 | `internal_server_error` | unexpected failure; the document is marked failed |

Re-uploading a file that was rejected returns the same error again (no new rows).

## CLI

```bash
# Ingest every file in a folder (non-recursive) under one pipeline_runs row.
docker compose run --rm api python -m app.cli ingest samples
# Any host folder: mount it into the container.
docker compose run --rm -v /path/to/exports:/in:ro api python -m app.cli ingest /in --country-code SG
```

Prints one line per file (`processed` / `duplicate` / `failed <file>: <error_code>`), then
`processed: N  duplicate: N  failed: N`. The run row's `stats` holds the same counts.

## Storage zones

- Raw zone: `data/raw/<sha256>.json`, the uploaded bytes, for every non-empty upload.
- Normalised zone: `data/normalised/<sha256>.v<normaliser_version>.json`. The `load`
  stage reads only this file, never the raw one.
- `./data` is mounted into the api container. Docker creates it as root; for a local
  (non-docker) run, `sudo chown -R $USER data` first.

## Tests

Tests run against the compose Postgres (each DB test module truncates the data tables;
raw/normalised files go to a temp dir, not `./data`).

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

Bbox coordinates slightly off the page (skewed scans) are clamped into [0, 1]; only a
box that is inverted after clamping is rejected. Textract LINE blocks without `Page`
get their page from the PAGE block that lists them as a CHILD.

Errors: non-JSON → `unreadable_file`; missing envelope keys, unknown engine, or a
raw_output that does not match the declared engine → `unsupported_ocr_format`.

## Known limitations

- `chunk_embeddings.embedding` is `vector(384)`; a model with another dimension
  needs a new migration. Both configured models are 384-dimensional.
