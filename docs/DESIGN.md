# Design notes

This document explains how the pipeline is built and why. It covers the storage
layers, the database schema, the normalised OCR format, the pipeline stages, and the
reasons behind each choice. It was written before the code. Decisions that changed
during implementation are listed in §11. How to run, use and extend the system is in
the [README](../README.md).

The pipeline takes OCR export files from three engines (AWS Textract, Tesseract and
Azure Document Intelligence). It converts each file into one normalised OCR JSON,
loads the result into PostgreSQL, splits the text into chunks, embeds them with
pgvector, and serves semantic search through an API. It handles documents of the
three supported types from any of the three engines, and rejects duplicates,
malformed files and unsupported formats with a clear error. Nothing in the code is
tied to the three sample files.

## 1. Stack (fixed)

- Python 3.12, `uv` or `pip` with `requirements.txt`.
- FastAPI + uvicorn (API), Typer (CLI), Pydantic v2 (all JSON schemas).
- PostgreSQL 16 with pgvector: docker image `pgvector/pgvector:0.8.0-pg16`.
- DB access: `psycopg[binary]` v3 and plain SQL (no ORM).
- Migrations are numbered SQL files in `migrations/`. `app/db/migrate.py` applies them
  and records each one in the table `schema_migrations(filename, applied_at)`.
- Embeddings: `sentence-transformers`. Default model `sentence-transformers/all-MiniLM-L6-v2`
  (384 dims). Second model for the re-embed demo: `BAAI/bge-small-en-v1.5` (384 dims).
- Raw zone and OCR zone: local disk under `./data/` (a docker volume). No S3.
- Tests: pytest, against the real Postgres from docker compose (`DATABASE_URL` env).
  Each test module truncates the tables in a fixture.
- Logging: stdlib `logging` with a PII-masking filter (see §8).

## 2. Repository layout

```
.
├── docs/DESIGN.md             # this file
├── README.md
├── docker-compose.yml         # db + api
├── Dockerfile                 # api image
├── requirements.txt
├── migrations/
│   ├── 001_initial.sql
│   └── 002_info_severity_and_country_error.sql
├── docker/postgres-init/      # creates the docs_test database on first start
├── scripts/generate_docs.py   # regenerates docs/normalised, docs/api, docs/quarantine.txt
├── docs/                      # diagrams, sample outputs, Postman collection
├── samples/                   # the three candidate-pack JSON files (synthetic)
├── data/                      # runtime: raw/, normalised/  (gitignored)
├── app/
│   ├── config.py              # env settings (DATABASE_URL, EMBEDDING_MODEL, thresholds)
│   ├── version.py             # CODE_VERSION (git short sha)
│   ├── main.py                # FastAPI app
│   ├── cli.py                 # Typer: ingest, reembed, migrate
│   ├── api/                   # routers: documents.py, search.py
│   ├── db/                    # migrate.py, connection.py, repositories (one file per table)
│   ├── ocr/
│   │   ├── schema.py          # NormalisedDocument pydantic models (§4)
│   │   ├── envelope.py        # parse + validate the export envelope
│   │   ├── registry.py        # engine name → normaliser function
│   │   ├── textract.py
│   │   ├── tesseract.py
│   │   └── azure_di.py
│   ├── pipeline/
│   │   ├── stages.py          # receive, normalise, load, extract, quality, chunk, embed
│   │   ├── storage.py         # raw and OCR zone files (atomic writes)
│   │   └── runner.py          # run_pipeline(file_bytes, filename, country_code) → result
│   ├── extraction/
│   │   ├── classify.py        # document_type from text (keyword rules)
│   │   ├── rules.py           # regex per field per document type
│   │   └── validate.py        # date / amount / provider_name / mc_days rules
│   ├── chunking.py
│   ├── embedding.py
│   └── security/pii.py        # log masking filter
└── tests/                     # pytest; tests/fixtures/ holds the four bad files
```

## 3. Relational schema (migration 001)

Conventions:
- All ids are `uuid` (`gen_random_uuid()`). All timestamps are `timestamptz`, default `now()`.
- Every derived row has `run_id` → `pipeline_runs.id` (lineage).
- All bounding boxes are `[x0, y0, x1, y1]`, as fractions of page width/height in [0, 1].
  The origin is the top-left corner.
- All confidences are in [0, 1].

A one-table-per-row summary is in the [README](../README.md#3-data-model-summary).
The diagram is [`erd.mmd`](erd.mmd).

### 3.1 pipeline_runs
One row per pipeline run: an API upload, one CLI ingest batch, or one reembed batch.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| run_type | text | `ingest` / `reembed` / `renormalise` |
| started_at, finished_at | timestamptz | |
| status | text | `running` / `succeeded` / `failed` / `partial` |
| code_version | text | git short sha, from env `CODE_VERSION`, else `unknown` |
| normaliser_version | text | `app.ocr.NORMALISER_VERSION` |
| embedding_model | text null | model name used in this run |
| embedding_model_version | text null | |
| stats | jsonb | processed / duplicate / failed counts |

### 3.2 documents
One row per unique uploaded export file. `content_hash` is the idempotency key.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| content_hash | text unique not null | `sha256:<hex>` of the uploaded bytes |
| source_sha256 | text null, indexed | From envelope `source.source_sha256`: the hash of the original scan. NOT unique: a re-OCR of the same scan is a new document row with the same value. Used to group "versions of the same scan" (design question 3). |
| upload_filename | text | filename as uploaded |
| source_filename | text null | envelope `source.original_filename` (the scan) |
| source_mime_type | text null | envelope `source.mime_type` |
| size_bytes | integer | |
| raw_storage_uri | text | `file://data/raw/<hex>.json` |
| normalised_storage_uri | text null | `file://data/normalised/<hex>.v<normaliser_version>.json` |
| document_type | text null | check in (`referral_letter`,`medical_certificate`,`receipt`) |
| country_code | char(2) not null | order: form field → envelope → `SG` (see §11) |
| ocr_engine | text null | envelope `ocr.engine` |
| ocr_engine_version | text null | |
| ocr_processed_at | timestamptz null | |
| status | text not null | see state machine §5 |
| error_code | text null | one of the API error codes when status = failed |
| error_message | text null | |
| ingested_at | timestamptz | |
| updated_at | timestamptz | |
| latest_run_id | uuid → pipeline_runs | |

### 3.3 document_stages
One row per document and stage. `GET /documents/{id}` returns these as the
pipeline status per stage. `timings_ms` comes from here.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents on delete cascade | |
| stage | text | `receive`,`normalise`,`load`,`extract`,`quality`,`chunk`,`embed` |
| status | text | `succeeded` / `failed` / `skipped` |
| started_at, finished_at | timestamptz | |
| duration_ms | integer | |
| error | text null | |
| run_id | uuid → pipeline_runs | |

Unique `(document_id, stage, run_id)`.

### 3.4 document_pages

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| page_number | integer | 1-based |
| width, height | numeric null | in `size_unit`; null when the engine gives none |
| size_unit | text null | `px` / `inch` / null |
| size_reason | text null | why width/height are null (Textract) |
| ocr_engine, ocr_engine_version | text | copied from the document, for direct querying |
| mean_confidence | numeric null | mean of line confidences, [0,1] |
| line_count | integer | |
| run_id | uuid → pipeline_runs | |

Unique `(document_id, page_number)`.

### 3.5 ocr_lines

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| page_id | uuid → document_pages cascade | |
| line_index | integer | 0-based, in the engine's reading order |
| text | text | |
| bbox_x0, bbox_y0, bbox_x1, bbox_y1 | numeric | normalised [0,1] |
| confidence | numeric null | [0,1] |
| confidence_source | text null | `engine_line` / `mean_of_words` |
| run_id | uuid → pipeline_runs | |

Unique `(page_id, line_index)`.

### 3.6 extracted_fields
Shape: key–value, one row per (document, field). There are 3 document types with 20
fields in total, and more types are expected. A key-value table keeps the schema the
same when a type is added: only `extraction/rules.py` changes. Each field also keeps
its own validation status, which typed columns cannot do cleanly. `normalised_value`
is jsonb, so one column can hold an int, string, bool or null.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| field_name | text | from appendix 8.1 (§6.1) |
| raw_value | text null | the OCR substring that matched |
| normalised_value | jsonb null | e.g. `"08/03/2026"`, `12500`, `true` |
| value_type | text | `date` / `amount` / `int` / `bool` / `text` / `datetime` |
| validation_status | text | `valid` / `invalid` / `missing` |
| validation_message | text null | |
| source_line_ids | uuid[] | ocr_lines the value came from (may be empty) |
| run_id | uuid → pipeline_runs | |

Unique `(document_id, field_name)`. Every field in the appendix list for the
document's type gets a row. It is `missing` when not found.

### 3.7 quality_checks

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| page_id | uuid null → document_pages cascade | null for document-level checks |
| check_name | text | `file_json`, `envelope_valid`, `engine_supported`, `document_type_supported`, `page_confidence`, `field_<name>` |
| passed | boolean | |
| severity | text | `error` (blocks the pipeline) / `warning` (flag only); `info` added later (§11) |
| details | jsonb | threshold, observed value, message |
| run_id | uuid → pipeline_runs | |

The full list of checks, including those added later, is in the
[README](../README.md#quality-checks).

### 3.8 chunks

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| page_id | uuid → document_pages cascade | chunks never cross pages |
| chunk_index | integer | 0-based within the document |
| chunking_version | text | `app.chunking.CHUNKING_VERSION` |
| text | text | |
| bbox_x0, bbox_y0, bbox_x1, bbox_y1 | numeric | union of the source line bboxes |
| source_line_ids | uuid[] | ordered |
| char_count | integer | |
| run_id | uuid → pipeline_runs | |

Unique `(document_id, chunking_version, chunk_index)`.

### 3.9 chunk_embeddings
Embeddings have their own table, so two models' vectors can exist for the same chunk.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| chunk_id | uuid → chunks cascade | |
| model_name | text | |
| model_version | text | sentence-transformers model revision/commit, else `unknown` |
| dimension | integer | |
| embedding | vector(384) | pgvector |
| created_at | timestamptz | |
| run_id | uuid → pipeline_runs | |

Unique `(chunk_id, model_name, model_version)`.
Index: `create index ... using hnsw (embedding vector_cosine_ops)`.
Distance metric: cosine. Search score = `1 - cosine_distance`.
The column is fixed at 384 dims, so a model with another dimension needs a new migration.
Both chosen models are 384. This is listed in the README's Known limitations.

### 3.10 embedding_models

| column | type | notes |
|---|---|---|
| model_name | text pk | |
| model_version | text | |
| dimension | integer | |
| is_active | boolean | exactly one row true; search uses the active model |
| created_at | timestamptz | |

`reembed --model X` inserts or updates the row, embeds everything, then flips
`is_active`. Until the flip, search keeps using the old model's rows.

### 3.11 View `v_quarantine`
Documents where `status = 'failed'`, or where any quality check did not pass.
Shows the error_code, the failed check names and the document's country and type.
Checks of severity `info` do not count (§11).

### 3.12 View `v_documents_deidentified` (PII control #2)
`documents` joined to `extracted_fields`. It shows document metadata and the
non-identifying fields only (amounts, dates, mc_days, provider_name). Rows for
`claimant_name`, `claimant_address` and `claimant_date_of_birth` are left out.

## 4. Normalised OCR JSON (Pydantic models in `app/ocr/schema.py`)

```json
{
  "normaliser_version": "1.1.0",
  "normalised_at": "2026-10-01T02:00:00Z",
  "coordinate_system": "fraction_of_page_0_1_origin_top_left",
  "confidence_scale": "0_1",
  "source": {
    "original_filename": "receipt.pdf",
    "mime_type": "application/pdf",
    "source_sha256": "…",
    "country_code": "SG"
  },
  "engine": { "name": "tesseract", "version": "5.3.4", "processed_at": "2026-09-02T03:12:44Z" },
  "pages": [
    {
      "page_number": 1,
      "width": 1654, "height": 2339, "size_unit": "px", "size_reason": null,
      "lines": [
        {
          "line_index": 0,
          "text": "MEDICAL CERTIFICATE",
          "bbox": [0.31, 0.05, 0.69, 0.08],
          "confidence": 0.97,
          "confidence_source": "mean_of_words",
          "bbox_clamped": false
        }
      ]
    }
  ]
}
```

Rules per engine. Each engine has a module `app/ocr/<engine>.py` with a pure function
`normalise(raw_output: dict) -> list[NormalisedPage]`. It raises
`UnsupportedFormatError` when `raw_output` does not have that engine's shape.

- **aws-textract**
  - Take the `Blocks` with `BlockType == "LINE"` and group them by `Page`.
  - bbox from `Geometry.BoundingBox` (Left, Top, Width, Height, already 0–1) →
    `[Left, Top, Left+Width, Top+Height]`.
  - confidence = `Confidence / 100`, `confidence_source = "engine_line"`.
  - width/height = null, size_unit = null,
    `size_reason = "aws-textract reports only ratio coordinates, no absolute page size"`.
  - Shape check: `Blocks` is a list with at least one block where `BlockType == "PAGE"`.
- **tesseract**
  - `raw_output` is the `image_to_data` dict of parallel arrays (`level`, `page_num`,
    `block_num`, `par_num`, `line_num`, `word_num`, `left`, `top`, `width`, `height`,
    `conf`, `text`).
  - Page size comes from the `level == 1` row (`width`, `height`, unit px).
  - A line is all `level == 5` rows with the same `(page_num, block_num, par_num, line_num)`
    and non-empty `text`. The words are joined with single spaces.
  - bbox = union of the word boxes, divided by page width/height.
  - confidence = mean of the word `conf` values, ignoring `-1`, divided by 100.
    `confidence_source = "mean_of_words"`. It is null if no word has a confidence.
  - Shape check: the dict has the keys `level`, `text`, `conf`, `left`, `top`, `width`,
    `height`, and they are lists of equal length.
- **azure-document-intelligence**
  - Read `raw_output.analyzeResult.pages[]`. Page width, height and unit are given
    (`unit` is `inch`).
  - For each `lines[]` entry, take the bbox from the 8-number `polygon`:
    `[min x, min y, max x, max y]`, divided by page width/height.
  - Line confidence = mean confidence of the `words[]` whose `span.offset` falls inside
    the line's `spans[0]` range (`line.offset <= word.span.offset < line.offset + line.length`).
    Note: a word has a single `span` object; a line has a `spans` list.
    `confidence_source = "mean_of_words"`.
  - Shape check: `analyzeResult.pages` exists and is a list; `analyzeResult.content` is a string.

Envelope validation (`app/ocr/envelope.py`):
- Required keys: `source`, `ocr.engine`, `raw_output`. `ocr.engine` must be one of the
  three names. `ocr.engine_version` and `ocr.processed_at` are copied when present.
- A file that is not JSON → `unreadable_file`.
- JSON without the envelope keys, an unknown engine name, or a raw_output that fails
  the declared engine's shape check → `unsupported_ocr_format`.

The normalised JSON is written to `data/normalised/<hex>.v<version>.json`, and its
URI is stored on the document. `load` reads this file and never reads raw again.

## 5. Document status state machine

`received → normalised → loaded → extracted → embedded`, or `failed` at any step.

| stage | on success status | failure error_code |
|---|---|---|
| receive (hash, raw store, file+envelope+engine checks) | received | file_missing / unreadable_file / unsupported_ocr_format / invalid_country_code (§11) |
| normalise | normalised | unsupported_ocr_format |
| load (pages, lines) | loaded | internal_server_error |
| extract (classify + fields + validation) | extracted | unsupported_document_type |
| quality (page confidence flags) | extracted (flags only, never fails) | — |
| chunk + embed | embedded | internal_server_error |

Rules:
- A file that fails at `receive` after the JSON parsed is still stored in the raw
  zone. It gets a `documents` row with `status = failed`. The assignment requires
  this for `unsupported_ocr_format` and `unsupported_document_type`.
- A file that is not valid JSON at all is also stored in the raw zone, with
  `status = failed` and `error_code = unreadable_file`.
- Duplicate: if the same `content_hash` is already present, no new rows are written
  anywhere. The existing document is returned with `duplicate: true`, HTTP 200.
- Every stage is a function `stage_x(conn, document_id, run_id) -> None`. It can be
  called on its own for an existing document; `reembed` relies on this.
- One document's exception never stops the batch. `runner.py` catches it, marks the
  document failed with `error_code = internal_server_error`, and continues.

## 6. Extraction (accuracy is not graded; shape is)

- `classify.py`: case-insensitive keyword rules on the joined page text. For example,
  "medical certificate" / "unfit for duty" → medical_certificate; "referral" →
  referral_letter; "receipt" / "tax invoice" / "gst" → receipt.
  No match → `unsupported_document_type`.
- `rules.py`: for each type, a dict `field_name → (regex, value_type)`.
  - The regex runs over the page text, built by joining lines with `\n`.
  - The ids of the ocr_lines whose text overlaps the match are kept.
  - In the samples, labels and values are often on separate lines (`Patient Name:` on
    one line, the name on the next). So every label regex must allow the value on the
    next line (`Label:\s*\n?\s*(value)`).
- `provider_name` is the clinic or provider that issued the document. The first
  non-empty line of the page is a reasonable default. It is never a line that only
  mentions "Fullerton Health". The samples all have a footer such as "Panel clinic of
  Fullerton Health network", and the hidden set likely does too.
- `validate.py`:
  - `date` must parse to `DD/MM/YYYY`; common input forms are accepted.
  - `amount`: strip currency symbols, separators and decimals → int
    (e.g. `S$1,234.50` → `123450`).
  - `int` (mc_days) must be a non-negative int.
  - `provider_name` is invalid if it contains "fullerton health" (case-insensitive).
  - Not found → `missing`. Parse failure → `invalid`, with raw_value kept.

### 6.1 Reference fields (Appendix 8.1)

The field list for each document type, and the formatting rule for each value, are
in the README: [Fields per document type](../README.md#fields-per-document-type) and
[Normalised values](../README.md#normalised-values).
`signature_presence` may be a simple check for the word "Signature" or "[signed]",
or null.

## 7. Chunking and embedding

- `CHUNKING_VERSION = "1.0"`. For each page, walk the lines in `line_index` order.
  Add lines until the next one would take the chunk past 400 characters; then emit
  the chunk.
- Overlap: the last line of a chunk is repeated as the first line of the next.
- A chunk never crosses a page. A page with fewer than 400 characters is one chunk.
  A single line longer than 400 characters becomes its own chunk.
- Reason: these are single-page, form-like documents. Line boundaries carry meaning
  (label: value), so chunks are built from whole lines. The small overlap keeps a
  label together with a value on the next line.
- Embedding: encode all chunk texts of a run in one call. Normalise the vectors
  (sentence-transformers `normalize_embeddings=True`).
- Page confidence threshold: `QUALITY_MIN_PAGE_CONFIDENCE` in config (0.80). The checks
  it drives are in the [README](../README.md#quality-checks).

## 8. PII control in code

Two controls: the log masking filter in `app/security/pii.py`, and the view
`v_documents_deidentified` (§3.12). What they cover is in the
[README](../README.md#9-pii-controls). Everything else is discussed in the report.

## 9. API and CLI contract

The field names are verbatim from the assignment. Requests, responses, error codes and
CLI commands are in the [README](../README.md#2-sample-curl-commands-and-cli-usage).

## 10. Tests (required set)

1. `test_idempotent_ingest`: ingest the same bytes twice under two filenames →
   one documents row, one set of pages/lines/chunks/embeddings, second response
   `duplicate: true`.
2. `test_normalise_textract` / `_tesseract` / `_azure`: each sample → page count,
   line count > 0, every bbox within [0,1], every confidence within [0,1] or null.
3. `test_validate_fields`: date forms, amount stripping, provider_name rule,
   mc_days rule.
4. `test_end_to_end`: ingest the three samples → search a query with a
   document_type filter → top result belongs to a document of that type.
5. `test_rejections`: non-JSON bytes → 422 unreadable_file; valid JSON with
   unknown engine → 422 unsupported_ocr_format and a failed documents row exists.

## 11. Decisions made during implementation

- **Amounts in the smallest currency unit.** §6 and the field rules gave conflicting
  examples (`S$1,234.50` → `1234` vs `S$93.20` → `9320`). Amounts are now integers in
  the smallest unit (`S$45` → `4500`; VND unscaled); see the README's Amounts table.
- **Country codes.** A malformed country code in the form field, or in the envelope
  with no valid form value, is rejected with 422 `invalid_country_code`. With a valid
  form value, the upload is accepted and gets an `envelope_country_code_invalid` warning.
- **Severity `info` for missing fields.** Quarantining every document with a missing
  field flagged too much. `field_<name>` is now `warning` when invalid and `info` when
  missing; only `error` and `warning` count for quarantine.
- **Normaliser version 1.1.0.** Lines gained `bbox_clamped`, set when a coordinate is
  clamped into [0, 1]. Since the normalised JSON changed, the version moved from 1.0.0
  to 1.1.0.
- **Day-first dates.** Numeric dates are read as DD/MM/YYYY, as written in SG, MY and
  VN. Philippine month-first dates can be misread; this is listed in the README's Known limitations.
