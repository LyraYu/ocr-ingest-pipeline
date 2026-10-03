# Document Ingestion & Embedding Pipeline

OCR engine exports (AWS Textract, Tesseract, Azure Document Intelligence) →
normalised OCR JSON → PostgreSQL → chunks + pgvector embeddings → search API.
The design spec is `CLAUDE.md`.

> Status: phase 3 (receive → normalise → load → extract → quality). Chunking,
> embeddings and search arrive in phase 4, so a document currently finishes with
> `status = extracted` and `chunks = 0`.

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
| 422 | `unsupported_document_type` | no document-type keyword in the text (stored + failed row) |
| 422 | `invalid_country_code` | `country_code` form field is not two letters |
| 404 | `not_found` | `GET /documents/{id}` for an unknown or malformed id |
| 500 | `internal_server_error` | unexpected failure; the document is marked failed |

Re-uploading a file that was rejected returns the same error again (no new rows).

## Extraction and quality

- `app/extraction/classify.py` picks `document_type` from weighted keywords (highest total
  weight wins; a tie goes to the earliest match). No keyword → `unsupported_document_type`.
- `app/extraction/rules.py` holds one `field_name → (regex, value_type)` dict per type; its
  keys are the CLAUDE.md §6.1 field list, and every field gets an `extracted_fields` row
  (`valid` / `invalid` / `missing`) with the `ocr_lines` ids the match came from.
  `provider_name` falls back to the first line of page 1, skipping lines that mention
  Fullerton Health.
- `app/extraction/validate.py` normalises values: dates → `DD/MM/YYYY`, `*_date_time` →
  `DD/MM/YYYY HH:MM`, amounts → integer in the smallest currency unit (`S$93.20` → `9320`,
  `S$45` → `4500`), `mc_days` → non-negative int.
- `quality_checks` per document: `file_json`, `envelope_valid`, `engine_supported` (receive),
  `document_type_supported` (extract), `page_confidence` per page plus one document-level
  row (threshold `QUALITY_MIN_PAGE_CONFIDENCE`, default 0.80), `bbox_clamped` per page,
  `field_<name>` per extracted field. Quality checks are warnings only and never fail the
  document. `quality_flags` in the POST response lists the names of checks that did not pass.

### Quarantine

Failed documents, plus any document with a check that did not pass:

```sql
select document_id, upload_filename, status, error_code, document_type, country_code, failed_checks
  from v_quarantine
 order by ingested_at desc;
```

```bash
docker compose exec db psql -U docs -d docs -c "select upload_filename, status, error_code, failed_checks from v_quarantine"
```

### How to add a new document type

Example: `discharge_summary`.

1. **`app/extraction/classify.py`**: add one entry to `KEYWORDS`, e.g.
   `"discharge_summary": {"discharge summary": 3, "date of admission": 1}`.
2. **`app/extraction/rules.py`**: add `FIELD_RULES["discharge_summary"] = {field_name: _rule(regex, value_type), ...}`.
   The keys are the type's field list. Reuse `labelled(labels, VALUE)` with `TEXT`, `DATE`,
   `DATETIME`, `AMOUNT`, `NUMBER`, and the shared `CLAIMANT_*` / `PROVIDER_NAME` rules.
   A new value_type needs a parser in `PARSERS_BY_TYPE` in `app/extraction/validate.py`;
   field-specific rules go in `PARSERS_BY_FIELD`.
3. **`CLAUDE.md` §6.1**: add the type and its field list.
4. **A new migration** `migrations/00N_document_type_<name>.sql`: `documents.document_type`
   has a check constraint listing the allowed types:
   ```sql
   alter table documents drop constraint documents_document_type_check;
   alter table documents add constraint documents_document_type_check
       check (document_type in ('referral_letter', 'medical_certificate', 'receipt', 'discharge_summary'));
   ```
   If the type has claimant fields with new names, also add them to the exclusion list in
   `v_documents_deidentified` (`create or replace view`).
5. **Tests**: a sample export in `tests/`, plus a case in `tests/test_extraction.py`.

No table changes are needed: `extracted_fields` is key-value.

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
