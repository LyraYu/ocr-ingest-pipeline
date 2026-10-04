# Design notes

This file is the source of truth for the implementation. If something is missing
or contradictory, ask before inventing.

Assignment: Fullerton Health take-home (Data Engineer). Three OCR engine exports
(AWS Textract, Tesseract, Azure Document Intelligence) → normalised OCR JSON →
PostgreSQL relational tables → chunks + embeddings (pgvector) → search API.
Hidden test set: more documents of the same 3 types in any of the 3 engine
formats, plus duplicates, malformed files and unsupported formats. Nothing may
be hard-coded to the three sample files.

## 1. Stack (fixed)

- Python 3.12, `uv` or `pip` with `requirements.txt`
- FastAPI + uvicorn (API), Typer (CLI), Pydantic v2 (all JSON schemas)
- PostgreSQL 16 with pgvector: docker image `pgvector/pgvector:pg16`
- DB access: `psycopg[binary]` v3 + plain SQL (no ORM). Migrations = numbered SQL
  files in `migrations/` applied by `app/db/migrate.py`, which records applied
  files in table `schema_migrations(filename, applied_at)`.
- Embeddings: `sentence-transformers`, default model `sentence-transformers/all-MiniLM-L6-v2`
  (384 dims). Second model for the re-embed demo: `BAAI/bge-small-en-v1.5` (384 dims).
- Raw zone / OCR zone: local disk under `./data/` (a docker volume). No S3.
- Tests: pytest. Tests run against the real Postgres from docker compose
  (`DATABASE_URL` env), each test module truncates tables in a fixture.
- Logging: stdlib `logging` with a PII-masking filter (see §8).

## 2. Repository layout

```
.
├── CLAUDE.md                  # this file
├── README.md
├── docker-compose.yml         # db + api
├── Dockerfile                 # api image
├── requirements.txt
├── migrations/
│   └── 001_initial.sql
├── samples/                   # the three candidate-pack JSON files (synthetic)
├── data/                      # runtime: raw/, normalised/  (gitignored)
├── app/
│   ├── config.py              # env settings (DATABASE_URL, EMBEDDING_MODEL, thresholds)
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
│   │   └── runner.py          # run_pipeline(file_bytes, filename, country_code) → result
│   ├── extraction/
│   │   ├── classify.py        # document_type from text (keyword rules)
│   │   ├── rules.py           # regex per field per document type
│   │   └── validate.py        # date / amount / provider_name / mc_days rules
│   ├── chunking.py
│   ├── embedding.py
│   └── security/pii.py        # log masking filter
└── tests/
```

## 3. Relational schema (migration 001)

Conventions: all ids are `uuid` (`gen_random_uuid()`), all timestamps `timestamptz`
default `now()`. Every derived row carries `run_id` → `pipeline_runs.id` (lineage).
All bounding boxes are `[x0, y0, x1, y1]` as fractions of page width/height in
[0, 1], origin top-left. All confidences are in [0, 1].

### 3.1 pipeline_runs
One row per invocation of the pipeline (an API upload, one CLI ingest batch, one
reembed batch).

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
| source_sha256 | text null, indexed | from envelope `source.source_sha256`; hash of the original scan. NOT unique: a re-OCR of the same scan is a new document row sharing this value. Used to group "versions of the same scan" (design question 3). |
| upload_filename | text | filename as uploaded |
| source_filename | text null | envelope `source.original_filename` (the scan) |
| source_mime_type | text null | envelope `source.mime_type` |
| size_bytes | integer | |
| raw_storage_uri | text | `file://data/raw/<hex>.json` |
| normalised_storage_uri | text null | `file://data/normalised/<hex>.v<normaliser_version>.json` |
| document_type | text null | check in (`referral_letter`,`medical_certificate`,`receipt`) |
| country_code | char(2) not null | resolution: form field → envelope → `SG` |
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
Per-document, per-stage record. This is what `GET /documents/{id}` returns as
"pipeline status per stage" and where `timings_ms` comes from.

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
| width, height | numeric null | in `size_unit`; null when engine gives none |
| size_unit | text null | `px` / `inch` / null |
| size_reason | text null | why width/height are null (Textract) |
| ocr_engine, ocr_engine_version | text | copied from document for direct querying |
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
| line_index | integer | 0-based, reading order as produced by the engine |
| text | text | |
| bbox_x0, bbox_y0, bbox_x1, bbox_y1 | numeric | normalised [0,1] |
| confidence | numeric null | [0,1] |
| confidence_source | text null | `engine_line` / `mean_of_words` |
| run_id | uuid → pipeline_runs | |

Unique `(page_id, line_index)`.

### 3.6 extracted_fields
Shape: key–value, one row per (document, field). Reason: 3 document types with
20 fields total and more types expected; a key-value table keeps the schema
stable when a type is added (only `extraction/rules.py` changes), and every field
carries its own validation status, which typed columns cannot do cleanly.
`normalised_value` is jsonb so it can hold int / string / bool / null with one
column.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| field_name | text | from appendix 8.1 |
| raw_value | text null | the OCR substring matched |
| normalised_value | jsonb null | e.g. `"08/03/2026"`, `12500`, `true` |
| value_type | text | `date` / `amount` / `int` / `bool` / `text` / `datetime` |
| validation_status | text | `valid` / `invalid` / `missing` |
| validation_message | text null | |
| source_line_ids | uuid[] | ocr_lines the value came from (may be empty) |
| run_id | uuid → pipeline_runs | |

Unique `(document_id, field_name)`. Every field in the appendix list for the
document's type gets a row, `missing` when not found.

### 3.7 quality_checks

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| page_id | uuid null → document_pages cascade | null for document-level checks |
| check_name | text | `file_json`, `envelope_valid`, `engine_supported`, `document_type_supported`, `page_confidence`, `field_<name>` |
| passed | boolean | |
| severity | text | `error` (blocks pipeline) / `warning` (flag only) |
| details | jsonb | threshold, observed value, message |
| run_id | uuid → pipeline_runs | |

### 3.8 chunks

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| document_id | uuid → documents cascade | |
| page_id | uuid → document_pages cascade | chunks never cross pages |
| chunk_index | integer | 0-based within document |
| chunking_version | text | `app.chunking.CHUNKING_VERSION` |
| text | text | |
| bbox_x0, bbox_y0, bbox_x1, bbox_y1 | numeric | union of source line bboxes |
| source_line_ids | uuid[] | ordered |
| char_count | integer | |
| run_id | uuid → pipeline_runs | |

Unique `(document_id, chunking_version, chunk_index)`.

### 3.9 chunk_embeddings
Embeddings live in their own table so two models can coexist for the same chunk.

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
Index: `create index ... using hnsw (embedding vector_cosine_ops)`. Distance metric:
cosine. Search score = `1 - cosine_distance`.
Known limitation (document in README): the column is fixed at 384 dims; a model
with another dimension needs a new migration. Both chosen models are 384.

### 3.10 embedding_models

| column | type | notes |
|---|---|---|
| model_name | text pk | |
| model_version | text | |
| dimension | integer | |
| is_active | boolean | exactly one row true; search uses the active model |
| created_at | timestamptz | |

`reembed --model X` inserts/updates the row, embeds everything, then flips
`is_active`. Until the flip, search keeps using the old model's rows.

### 3.11 View `v_quarantine`
Documents where `status = 'failed'` OR any `quality_checks.passed = false`,
with error_code, failed check names and the document's country/type.

### 3.12 View `v_documents_deidentified` (PII control #2)
`documents` joined to `extracted_fields`, exposing document metadata and
non-identifying fields only (amounts, dates, mc_days, provider_name); rows for
`claimant_name`, `claimant_address`, `claimant_date_of_birth` are excluded.

## 4. Normalised OCR JSON (Pydantic models in `app/ocr/schema.py`)

```json
{
  "normaliser_version": "1.0.0",
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
          "confidence_source": "mean_of_words"
        }
      ]
    }
  ]
}
```

Rules per engine (`app/ocr/<engine>.py`, each a pure function
`normalise(raw_output: dict) -> list[NormalisedPage]`; raise
`UnsupportedFormatError` when `raw_output` does not have that engine's shape):

- **aws-textract** — iterate `Blocks` with `BlockType == "LINE"`, group by `Page`.
  bbox from `Geometry.BoundingBox` (Left, Top, Width, Height, already 0–1) →
  `[Left, Top, Left+Width, Top+Height]`. confidence = `Confidence / 100`,
  `confidence_source = "engine_line"`. width/height = null, size_unit = null,
  `size_reason = "aws-textract reports only ratio coordinates, no absolute page size"`.
  Shape check: `Blocks` is a list and contains at least one block with `BlockType == "PAGE"`.
- **tesseract** — `raw_output` is the `image_to_data` dict of parallel arrays
  (`level`, `page_num`, `block_num`, `par_num`, `line_num`, `word_num`, `left`,
  `top`, `width`, `height`, `conf`, `text`). Page size from the `level == 1` row
  (`width`, `height`, unit px). A line = all `level == 5` rows sharing
  `(page_num, block_num, par_num, line_num)` with non-empty `text`; text joined
  with single spaces; bbox = union of word boxes divided by page width/height;
  confidence = mean of word `conf` values, ignoring `-1`, divided by 100;
  `confidence_source = "mean_of_words"`; null if no words have conf.
  Shape check: the dict has the keys `level`, `text`, `conf`, `left`, `top`,
  `width`, `height` and they are equal-length lists.
- **azure-document-intelligence** — `raw_output.analyzeResult.pages[]`. Page
  width/height/unit given (`unit` is `inch`). For each `lines[]` entry: bbox from
  the 8-number `polygon` → `[min x, min y, max x, max y]` divided by page
  width/height. Line confidence = mean of the confidences of the `words[]` whose
  `span.offset` (NOTE: a word has a single `span` object; a line has a `spans`
  list) falls inside the line's `spans[0]` range
  (`line.offset <= word.span.offset < line.offset + line.length`);
  `confidence_source = "mean_of_words"`.
  Shape check: `analyzeResult.pages` exists and is a list; `analyzeResult.content` is a string.

Envelope validation (`app/ocr/envelope.py`): required keys `source`, `ocr.engine`,
`raw_output`; `ocr.engine` must be one of the three names; `ocr.engine_version`
and `ocr.processed_at` copied through when present. A file that is not JSON →
`unreadable_file`. JSON without the envelope keys, an unknown engine name, or a
raw_output that fails the declared engine's shape check → `unsupported_ocr_format`.

The normalised JSON is written to `data/normalised/<hex>.v<version>.json` and its
URI stored on the document. `load` reads from this file, never from raw again.

## 5. Document status state machine

`received → normalised → loaded → extracted → embedded`, or `failed` at any step.

| stage | on success status | failure error_code |
|---|---|---|
| receive (hash, raw store, file+envelope+engine checks) | received | file_missing / unreadable_file / unsupported_ocr_format |
| normalise | normalised | unsupported_ocr_format |
| load (pages, lines) | loaded | internal_server_error |
| extract (classify + fields + validation) | extracted | unsupported_document_type |
| quality (page confidence flags) | extracted (flags only, never fails) | — |
| chunk + embed | embedded | internal_server_error |

Rules:
- A file that fails at `receive` after the JSON parsed is still stored in the raw
  zone and gets a `documents` row with `status = failed` (required by the spec for
  `unsupported_ocr_format` and `unsupported_document_type`). A file that is not
  valid JSON at all is stored in raw zone too, with `status = failed`,
  `error_code = unreadable_file`.
- Duplicate: same `content_hash` already present → no new rows anywhere, return the
  existing document with `duplicate: true`, HTTP 200.
- Every stage is a function `stage_x(conn, document_id, run_id) -> None` that can
  be called on its own for an existing document (this is what `reembed` relies on).
- One document's exception never stops the batch: `runner.py` catches, marks the
  document failed with `error_code = internal_server_error`, and continues.

## 6. Extraction (accuracy is not graded; shape is)

- `classify.py`: keyword rules on the concatenated page text, case-insensitive;
  e.g. "medical certificate" / "unfit for duty" → medical_certificate; "referral"
  → referral_letter; "receipt" / "tax invoice" / "gst" → receipt. No match →
  `unsupported_document_type`.
- `rules.py`: per type, a dict `field_name → (regex, value_type)`. Regex runs over
  the page text built by joining lines with `\n`; also keep the ocr_line ids
  whose text overlaps the match. Labels and values are often on separate
  lines in the samples (`Patient Name:` on one line, the name on the next), so
  every label regex must allow the value to follow on the next line
  (`Label:\s*\n?\s*(value)`).
- `provider_name` is the clinic/provider that issued the document (the first
  non-empty line of the page is a reasonable default), never a line that
  merely mentions "Fullerton Health" (the samples all carry a footer such as
  "Panel clinic of Fullerton Health network"; the hidden set likely does too).
- `validate.py`: `date` → must parse to `DD/MM/YYYY` (accept common input forms,
  output that string); `amount` → strip currency symbols, separators, decimals →
  int (e.g. `S$1,234.50` → `123450`); `int` (mc_days) → non-negative int;
  `provider_name` → invalid if it contains "fullerton health" (case-insensitive).
  Missing → `missing`; parse failure → `invalid` with raw_value kept.

### 6.1 Reference fields (Appendix 8.1)

```
referral_letter: claimant_name, provider_name, signature_presence (bool),
  total_amount_paid, total_approved_amount, total_requested_amount
medical_certificate: claimant_name, claimant_address, claimant_date_of_birth,
  diagnosis_name, discharge_date_time, icd_code, provider_name,
  submission_date_time, date_of_mc, mc_days (int)
receipt: claimant_name, claimant_address, claimant_date_of_birth,
  provider_name, tax_amount, total_amount
```

Formatting rules: dates as DD/MM/YYYY; *_date_time fields keep "DD/MM/YYYY HH:MM";
amounts as integers with all currency symbols, separators and decimals
removed (S$93.20 → 9320, SGD 1,200.00 → 120000); provider_name must not
contain "Fullerton Health"; mc_days is a non-negative integer;
signature_presence may be a simple heuristic (presence of the word
"Signature" or "[signed]") or null.

## 7. Chunking and embedding

- `CHUNKING_VERSION = "1.0"`. Per page: walk lines in `line_index` order,
  accumulate until adding the next line would exceed 400 characters, then emit a
  chunk. Overlap: the last line of a chunk is repeated as the first line of the
  next. Never cross a page. A page with fewer than 400 chars is one chunk.
  Reason: these are single-page form-like documents; line boundaries carry
  meaning (label: value), so chunks are built from whole lines, and a small
  overlap keeps a label together with a value split across lines.
- Embedding: batch encode all chunk texts of a run in one call; normalise
  vectors (sentence-transformers `normalize_embeddings=True`).
- Quality threshold: page `mean_confidence < 0.80` → `quality_checks` warning
  `page_confidence`; also a document-level warning if any page is flagged.
  The threshold is `QUALITY_MIN_PAGE_CONFIDENCE` in config.

## 8. PII control in code

`app/security/pii.py`: a `logging.Filter` that masks NRIC/FIN-like ids
(`[STFG]\d{7}[A-Z]`), Vietnamese/Philippine id-like digit runs (9–12 digits),
dates of birth and any value tagged as a claimant field before it reaches a log
record. Applied to the root logger in `main.py` and `cli.py`. Plus view
`v_documents_deidentified` (§3.12). Everything else is discussed in the report.

## 9. API and CLI contract (verbatim field names from the assignment)

- `POST /documents` multipart `file`, optional `country_code` → 201 (new) / 200
  (duplicate) with `{document_id, content_hash, duplicate, document_type, status,
  pages, chunks, quality_flags, timings_ms{normalise, load, embed}}`.
  Errors: 400 `file_missing`, 422 `unreadable_file` / `unsupported_ocr_format` /
  `unsupported_document_type`, 500 `internal_server_error`. Body `{"error": "<code>"}`.
- `GET /documents/{id}` → document metadata, `stages` (from document_stages),
  `quality_checks`, `extracted_fields`, `pages`. 404 `not_found`.
- `POST /search` `{query, top_k, filters{document_type?, country_code?}}` →
  `{results:[{chunk_id, document_id, document_type, page, score, text, bbox,
  embedding_model}]}` using the active model; only `chunk_embeddings` rows of
  that model are searched.
- CLI: `python -m app.cli ingest <folder>` (prints processed / duplicate /
  failed), `python -m app.cli reembed --model <name>`, `python -m app.cli migrate`.

## 10. Tests (minimum)

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
