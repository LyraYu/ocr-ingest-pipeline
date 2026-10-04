# Document Ingestion & Embedding Pipeline

This pipeline ingests OCR exports from three engines (AWS Textract, Tesseract, Azure
Document Intelligence). Each export is normalised into one OCR JSON format, loaded into
PostgreSQL, chunked and embedded with pgvector, and made searchable through an API.

Each document goes through these stages: receive → normalise → load → extract → quality →
chunk → embed. A successful document ends with `status = embedded`.

Report materials are in [`docs/`](docs/): the architecture and ER diagrams, sample
normalised JSON, real API responses, the quarantine view and its output, and a Postman
collection.

## 1. Setup and run

Requirements: Docker with Compose v2. Nothing else is needed on the host: Python, the
database and the embedding model all run in containers.

### Quick start

```bash
docker compose up -d
docker compose run --rm api python -m app.cli ingest samples
POST=$(curl -s -F file=@samples/receipt.json localhost:8000/documents); echo "$POST"
DOC_ID=$(echo "$POST" | grep -o '"document_id":"[^"]*"' | cut -d'"' -f4)
curl -s localhost:8000/documents/$DOC_ID
curl -s localhost:8000/search -H 'content-type: application/json' -d '{"query": "medical leave for acute gastroenteritis", "top_k": 3, "filters": {"document_type": "medical_certificate"}}'
```

1. `docker compose up -d` builds the api image on first use, which takes a few minutes: it
   installs CPU-only torch and downloads the default embedding model. It then starts:
   - `db`: Postgres 16 with pgvector, on port 5432;
   - `api`: applies migrations, then serves on port 8000.
2. The second line is the seed command. It ingests the three files in `samples/`, applying
   any pending migrations first. It prints one line per file, then
   `processed: 3  duplicate: 0  failed: 0`.
3. The `POST /documents` line uploads a sample that is already ingested. The API returns 200
   with `"duplicate": true`, which is printed. The next line keeps its `document_id` in
   `DOC_ID`.
4. The last two lines fetch that document and run a search.

To stop the stack: `docker compose down`. To also delete the database and the model cache:
`docker compose down -v`.

### Configuration

The `api` service reads these environment variables (see `docker-compose.yml` and
`.env.example`):

| variable | default | meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://docs:docs@db:5432/docs` | application database |
| `DATA_DIR` | `/app/data` (mounted from `./data`) | raw and normalised zones |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | model for the first ingest, before any model is active |
| `QUALITY_MIN_PAGE_CONFIDENCE` | `0.80` | page confidence threshold |
| `CODE_VERSION` | (empty) | see below |

### Code version

Every `pipeline_runs` row records `code_version`, the git short sha of the code that ran.
- When the image is built (`app/version.py`), the value is taken from the `CODE_VERSION`
  build arg if set. Otherwise it is read from `.git/HEAD` in the build context; no git
  binary is needed. The value is written to `/app/CODE_VERSION`.
- At run time, a non-empty `CODE_VERSION` environment variable overrides the file.
- An image built outside a git checkout (e.g. from a tarball) records `unknown`, unless the
  build arg is set:

```bash
CODE_VERSION=$(git rev-parse --short HEAD) docker compose build api
```

The sha is that of the checked-out commit; uncommitted changes are not reflected.

### Tests

```bash
docker compose up -d db
docker compose build api                         # after code changes
docker compose run --rm api pytest -v
```

Tests run against the `docs_test` database on the compose Postgres:
- The db service creates `docs_test` when its volume is first initialised
  (`docker/postgres-init/`). `tests/conftest.py` also creates it if missing.
- Tests never touch `docs`; conftest refuses to run if the two names would be the same.
- Each DB test module truncates the `docs_test` tables.
- Raw and normalised files go to a temp directory, not `./data`.
- The reembed test downloads `BAAI/bge-small-en-v1.5` (~130 MB) into the `hf-cache` volume
  the first time it runs.

To run tests outside Docker (Python 3.12):

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && set -a && . ./.env && set +a   # DATABASE_URL → localhost:5432/docs
pytest -v                                                # uses localhost:5432/docs_test
```

### Migrations

Numbered SQL files in `migrations/` are applied in filename order by
`python -m app.cli migrate`. The api container runs this on start, and `ingest` runs it
before ingesting. Each file runs in one transaction and is recorded in
`schema_migrations(filename, applied_at)`.

### Regenerating docs/

```bash
docker compose run --rm -v "$PWD/docs:/app/docs" api python -m scripts.generate_docs
```

This writes `docs/normalised/`, `docs/api/` and `docs/quarantine.txt`. It works in its own
database, `docs_report`, which it drops and recreates on each run. The other files in
`docs/` are written by hand.

## 2. Sample curl commands and CLI usage

### POST /documents

```bash
curl -s -F file=@samples/receipt.json -F country_code=SG localhost:8000/documents
```

Multipart field `file` (required) and `country_code` (optional). The country code is
resolved as: form field → envelope `source.country_code` → `SG`. The response is 201 for a
new document, or 200 with `"duplicate": true` if the same bytes were uploaded before:

```json
{"document_id": "8ee20b17-…", "content_hash": "sha256:00fce337…", "duplicate": false,
 "document_type": "receipt", "status": "embedded", "pages": 1, "chunks": 2,
 "quality_flags": [], "timings_ms": {"normalise": 2, "load": 3, "embed": 24}}
```

Real responses: `docs/api/post_documents_<type>.json`.

### GET /documents/{id}

```bash
curl -s localhost:8000/documents/<document_id>
```

Returns:
- the document's metadata;
- `stages`: per-stage status and `duration_ms`;
- `quality_checks`;
- `extracted_fields`: raw value, normalised value, validation status and source line ids;
- `pages`.

Real responses: `docs/api/get_documents_<type>.json`.

### POST /search

```bash
curl -s localhost:8000/search -H 'content-type: application/json' -d '{
  "query": "medical leave for acute gastroenteritis",
  "top_k": 5,
  "filters": {"document_type": "medical_certificate", "country_code": "SG"}
}'
```

Request fields:
- `query`: required.
- `top_k`: integer 1–50, default 5.
- `filters`: optional. `document_type` is one of `referral_letter`,
  `medical_certificate`, `receipt`; `country_code` is two letters, case-insensitive.

Each result has `chunk_id`, `document_id`, `document_type`, `page`, `score`, `text`,
`bbox` and `embedding_model`:
- `score` is `1 - cosine distance`, so higher means more similar;
- `bbox` is the union of the chunk's line boxes, as page fractions.

Only embeddings of the active model are searched. Real requests and responses:
`docs/api/search_1.json` … `search_3.json`.

### Errors

Every error has the body `{"error": "<code>"}`:

| HTTP | code | when |
|---|---|---|
| 400 | `file_missing` | no `file` part, or an empty file |
| 422 | `unreadable_file` | not JSON |
| 422 | `unsupported_ocr_format` | no envelope, unknown `ocr.engine`, or `raw_output` not in that engine's shape |
| 422 | `unsupported_document_type` | no document-type keyword in the text |
| 422 | `invalid_country_code` | the form field is not two letters; or the envelope's `source.country_code` is not two letters and no valid form value was sent |
| 422 | `invalid_request` | request body or parameters fail validation (any endpoint); `detail` lists the problems |
| 404 | `not_found` | `GET /documents/{id}` for an unknown or malformed id |
| 500 | `internal_server_error` | unexpected failure; the document is marked failed |

For `unreadable_file`, `unsupported_ocr_format`, `unsupported_document_type` and an
envelope `invalid_country_code`, the file is still kept in the raw zone, with a
`documents` row that has `status = failed`. Re-uploading the same file returns the same
error and writes no new rows.

Postman: import `docs/postman_collection.json`. The POST request saves `document_id` for
the GET request.

### CLI

```bash
# Ingest every file in a folder (non-recursive) under one pipeline run
docker compose run --rm api python -m app.cli ingest samples
docker compose run --rm -v /path/to/exports:/in:ro api python -m app.cli ingest /in --country-code SG

# Re-embed all documents with another model, then switch search to it
docker compose run --rm api python -m app.cli reembed --model BAAI/bge-small-en-v1.5

# Apply pending migrations
docker compose run --rm api python -m app.cli migrate
```

`ingest` prints one line per file (`processed` / `duplicate` /
`failed <file>: <error_code>`), then `processed: N  duplicate: N  failed: N`. Each file
goes through receive → chunk; then all new chunks of the batch are embedded in one encode
call.

`reembed` prints the document counts, the new model's embedding count and the active
model. It exits with status 1 if any document failed; in that case the active model is
left unchanged.

## 3. Data model summary

Diagram: [`docs/erd.mmd`](docs/erd.mmd) (Mermaid erDiagram). Pipeline and storage layers:
[`docs/architecture.mmd`](docs/architecture.mmd).

| table | one row per | notes |
|---|---|---|
| `pipeline_runs` | API upload, CLI ingest batch, or reembed | run type, status, `code_version`, `normaliser_version`, embedding model, processed/duplicate/failed counts |
| `documents` | unique uploaded file | `content_hash` (sha256 of the bytes) is unique: the idempotency key. `source_sha256` (hash of the original scan) is indexed but not unique, so re-OCRs of one scan group together. Holds status, error code, type, country, engine and storage URIs |
| `document_stages` | document × stage × run | status, `duration_ms`, error |
| `document_pages` | page | size and unit (null for Textract, with `size_reason`), mean line confidence, line count |
| `ocr_lines` | OCR line | text, bbox, confidence and its source |
| `extracted_fields` | document × field | key-value: raw value, `normalised_value` (jsonb), validation status, source line ids |
| `quality_checks` | check that ran | check name, page (or null), passed, severity, details |
| `chunks` | chunk | text, union bbox, ordered source line ids, `chunking_version` |
| `chunk_embeddings` | chunk × model × model version | `vector(384)`, HNSW cosine index |
| `embedding_models` | model | exactly one `is_active` |

Every derived row has a `run_id`, so each value can be traced to the run that produced it
and that run's code and normaliser versions. Deleting a document cascades to all of its
rows.

Extracted fields use a key-value table, not one column per field. Adding a document type
then needs no table change, and every field keeps its own validation status.

Storage zones on local disk (the `./data` volume):
- raw zone: `data/raw/<sha256>.json`, the uploaded bytes, for every non-empty upload;
- OCR zone: `data/normalised/<sha256>.v<normaliser_version>.json`.

The `load` stage reads only the normalised file, never the raw one.

## 4. Normalised OCR format and bbox coordinate system

All three engines are converted to one format (`app/ocr/schema.py`). The full output for
each sample is in `docs/normalised/<type>.json`; the excerpt below is from the receipt,
with numbers rounded.

```json
{
  "normaliser_version": "1.1.0",
  "normalised_at": "2026-10-03T17:39:55.328914Z",
  "coordinate_system": "fraction_of_page_0_1_origin_top_left",
  "confidence_scale": "0_1",
  "source": {"original_filename": "receipt.pdf", "mime_type": "application/pdf",
             "source_sha256": "…", "country_code": "SG"},
  "engine": {"name": "tesseract", "version": "5.3.4", "processed_at": "2026-09-05T06:40:02Z"},
  "pages": [{
    "page_number": 1, "width": 1240.0, "height": 1754.0, "size_unit": "px", "size_reason": null,
    "lines": [{"line_index": 0, "text": "NOVENA WELLNESS MEDICAL CENTRE",
               "bbox": [0.0806, 0.0627, 0.4629, 0.0798], "confidence": 0.9585,
               "confidence_source": "mean_of_words", "bbox_clamped": false}]
  }]
}
```

### Bounding boxes

A bbox is `[x0, y0, x1, y1]`:
- `x` values are fractions of the page width, `y` values fractions of the page height, all
  in [0, 1];
- the origin `(0, 0)` is the top-left corner of the page, and `(1, 1)` the bottom-right;
- `x0 ≤ x1` and `y0 ≤ y1`.

To get pixels on a page image of size W × H, multiply x by W and y by H.

Coordinates slightly outside the page (for example from a skewed scan) are clamped into
[0, 1], and the line gets `bbox_clamped: true`. Only a box that is still inverted after
clamping is rejected. Confidences are in [0, 1], or null when the engine gives none.

### Per-engine rules

| engine | bbox | page size | line confidence |
|---|---|---|---|
| aws-textract | `Geometry.BoundingBox` (already 0–1) → `[Left, Top, Left+Width, Top+Height]` | null, with `size_reason` (Textract reports only ratios) | LINE `Confidence / 100` (`engine_line`) |
| tesseract | union of the line's word boxes ÷ page width/height | level-1 row, `px` | mean of word `conf`, ignoring -1, / 100 (`mean_of_words`) |
| azure-document-intelligence | min/max of the line `polygon` ÷ page width/height | page `width`/`height`, `inch` (`pixel` → `px`) | mean confidence of the words whose `span.offset` is inside the line's `spans[0]` (`mean_of_words`) |

For Textract, a LINE block without `Page` gets its page from the PAGE block that lists it as
a CHILD.

### Normaliser versions

`NORMALISER_VERSION` (in `app/ocr/__init__.py`) must change whenever the normalised JSON
changes: either its shape, or the values a given input produces. The version is part of the
normalised file name and is recorded on every `pipeline_runs` row. So files from different
versions sit side by side, and every row can be traced to the normaliser that produced it.

Worked example, **1.0.0 → 1.1.0**: lines gained the field `bbox_clamped`, which feeds the
`bbox_clamped` quality check. This is an additive schema change, so it is a minor version
bump. Documents normalised by 1.0.0 keep their `.v1.0.0.json` files. To move a document to
1.1.0, re-run `stage_normalise` and the later stages for it; this writes a `.v1.1.0.json`
file next to the old one.

### Chunks

`app/chunking.py`, `CHUNKING_VERSION = "1.0"`:
- Chunks are built from whole lines of one page (never across pages), up to 400 characters,
  with lines joined by `\n`.
- The last line of a chunk is repeated as the first line of the next, so a `Label:` line
  stays next to its value.
- A single line longer than 400 characters becomes its own chunk.

## 5. How to add a new document type

Example: `discharge_summary`.

1. **`app/extraction/classify.py`**: add one entry to `KEYWORDS`, e.g.
   `"discharge_summary": {"discharge summary": 3, "date of admission": 1}`.
   - Weights: title phrases count more than incidental words.
   - The type with the highest total weight wins; a tie goes to the earliest match.
2. **`app/extraction/rules.py`**: add `FIELD_RULES["discharge_summary"] = {field_name: _rule(regex, value_type), ...}`.
   - The keys are the type's field list. Every key gets an `extracted_fields` row, which
     is `missing` when no rule matches.
   - Build regexes with `labelled(labels, VALUE)`, using the value patterns `TEXT`, `DATE`,
     `DATETIME`, `AMOUNT` and `NUMBER`. A label must start a line; the value may be on the
     same line or the next one.
   - The shared `CLAIMANT_*` and `PROVIDER_NAME` rules can be reused.
3. **`app/extraction/validate.py`**: only if needed. A new value type needs a parser in
   `PARSERS_BY_TYPE`; a field-specific rule goes in `PARSERS_BY_FIELD`.
4. **A new migration**, `migrations/00N_document_type_<name>.sql`. `documents.document_type`
   has a check constraint listing the allowed types:
   ```sql
   alter table documents drop constraint documents_document_type_check;
   alter table documents add constraint documents_document_type_check
       check (document_type in ('referral_letter', 'medical_certificate', 'receipt', 'discharge_summary'));
   ```
   If the type has claimant fields with new names, add them to the exclusion list of
   `v_documents_deidentified` (`create or replace view`).
5. **`app/api/search.py`**: add the type to the `document_type` filter's `Literal`.
6. **Docs**: add the type and its field list to "Fields per document type" in section 8 of
   this README, and to `docs/DESIGN.md` §6.1.
7. **Tests**: a sample export, and a case in `tests/test_extraction.py`.

No table changes are needed.

## 6. How to add a new OCR engine format

Example: Google Document AI, engine name `google-document-ai`.

1. **`app/ocr/google_docai.py`**, following `textract.py`:
   - `check_shape(raw_output: dict) -> None`: a quick structural check that raises
     `UnsupportedFormatError` when `raw_output` is not this engine's shape.
   - `normalise(raw_output: dict) -> list[NormalisedPage]`, decorated with
     `@raises_unsupported_format(ENGINE)`.
   - Each page has a 1-based `page_number`, and `width`/`height`/`size_unit` (or all null,
     plus a `size_reason`).
   - Each page's `lines` are in reading order. Each line has `line_index` 0..n-1, `text`,
     `bbox` as page fractions, `confidence` in [0, 1] or null, and `confidence_source`.
   - Read numbers with `as_number()`, so malformed values become named errors.
2. **`app/ocr/registry.py`**: add
   `google_docai.ENGINE: EngineAdapter(google_docai.check_shape, google_docai.normalise)`
   to `ENGINES`. Envelope validation accepts every name in `ENGINES`.
3. **`app/ocr/schema.py`**: add the name to the `EngineName` literal.
4. **`NORMALISER_VERSION`**: bump the minor version.
5. **Tests**:
   - add a sample export to `samples/` (fixtures key samples by `ocr.engine`);
   - add a `test_normalise_<engine>` in `tests/test_normalisers.py`;
   - add a branch in `test_malformed_raw_output_is_named_error`.

   `test_wrong_engine_raw_output_is_rejected` is parametrised over `ENGINES` and picks up
   the new engine automatically.
6. **Docs**: add the engine's rules to the per-engine table in section 4 of this README,
   and to `docs/DESIGN.md` §4.

Nothing downstream changes: `load` and every later stage read only the normalised JSON.

## 7. How to switch the embedding model and re-embed

Embeddings are stored in `chunk_embeddings`, keyed by `(chunk_id, model_name, model_version)`,
so several models' vectors can exist for the same chunk. `embedding_models` has exactly
one active row. Search embeds the query with that model and reads only that model's rows.

1. Run `python -m app.cli reembed --model <hub id or local path>`.
   - The model must produce 384-dimensional vectors.
   - It is loaded from the Hugging Face cache (the `hf-cache` volume), or downloaded if
     absent.
   - `model_version` is the hub commit of the loaded snapshot.
2. One `pipeline_runs` row (`run_type = reembed`) covers the whole operation. For every
   document with `status = embedded`:
   - `stage_chunk` reuses the existing chunks;
   - `stage_embed` adds one row per chunk that has none for this model and version.

   All chunks are encoded in one call. Rows of other models are not touched, and document
   status stays `embedded`.
3. `is_active` switches to the new model only if every document succeeded. Until then,
   search keeps using the old model. If anything failed, re-running `reembed` embeds only
   the missing chunks.
4. To switch back, run `reembed --model <old model>`. Its rows still exist, so nothing is
   re-encoded; only the active flag moves.

New uploads are embedded with the active model. `EMBEDDING_MODEL` is used only for the
first ingest, before any model is active.

To delete an old model's vectors once they are no longer needed:

```sql
delete from chunk_embeddings where model_name = 'sentence-transformers/all-MiniLM-L6-v2';
delete from embedding_models where model_name = 'sentence-transformers/all-MiniLM-L6-v2' and not is_active;
```

## 8. Data quality and quarantine

### Extraction and validation

Every field in the type's list gets an `extracted_fields` row, with `validation_status`
`valid`, `invalid` (the raw value is kept and a message is set) or `missing`.

#### Fields per document type

| document type | fields |
|---|---|
| `referral_letter` | `claimant_name`, `provider_name`, `signature_presence` (bool), `total_amount_paid`, `total_approved_amount`, `total_requested_amount` |
| `medical_certificate` | `claimant_name`, `claimant_address`, `claimant_date_of_birth`, `diagnosis_name`, `discharge_date_time`, `icd_code`, `provider_name`, `submission_date_time`, `date_of_mc`, `mc_days` (int) |
| `receipt` | `claimant_name`, `claimant_address`, `claimant_date_of_birth`, `provider_name`, `tax_amount`, `total_amount` |

The lists are the keys of `FIELD_RULES` in `app/extraction/rules.py`.

#### Normalised values

| value | normalised form |
|---|---|
| date | `DD/MM/YYYY`. Accepts D/M/YYYY with `/` `.` or `-`, YYYY-MM-DD, "8 Mar 2026", "Mar 8, 2026" |
| `*_date_time` | `DD/MM/YYYY HH:MM` (24-hour) |
| amount | integer in the currency's smallest unit (see below) |
| `mc_days` | non-negative integer (`-1` and `2.5` are invalid) |
| `provider_name` | text; invalid if it contains "Fullerton Health" |
| `signature_presence` | `true` when "Signature" or "[signed]" appears, otherwise `missing` |

`provider_name` comes from an explicit label such as "Referring provider:". Without one, it
is the first line of page 1, skipping any line that mentions Fullerton Health.

### Amounts

Currency symbols, thousands separators and the decimal point are removed, and the result
is an integer in the currency's smallest unit. An amount written without decimals is scaled
the same way, so all values of one currency share a unit.

| input | stored |
|---|---|
| `S$93.20` | `9320` |
| `SGD 1,200.00` | `120000` |
| `S$45` | `4500` |
| `$8.5` | `850` |
| `VND 1,200,000` | `1200000` |

| currency (as written) | minor-unit digits |
|---|---|
| no symbol, `$`, `S$`, `SGD`, `US$`, `USD`, `RM`, `MYR`, `PHP`, `₱` | 2 |
| `VND`, `₫` | 0 (not scaled) |

Any other currency, a negative amount, or more decimals than the currency allows is
`invalid`. The table is `CURRENCY_EXPONENT` in `app/extraction/validate.py`.

### Quality checks

Each `quality_checks` row is a check that ran:

| check | written by | severity | does not pass when |
|---|---|---|---|
| `file_json` → `envelope_valid` → `engine_supported` | receive, in this order; checks after a failure did not run and have no row | error | not JSON / envelope keys missing / unknown engine or `raw_output` shape |
| `envelope_country_code_invalid` | receive; written only when it occurs | warning | the envelope's country code is malformed and the valid form value was used instead |
| `document_type_supported` | extract | error | no document-type keyword |
| `page_confidence` | quality: one row per page, plus one document-level row | warning | mean line confidence < `QUALITY_MIN_PAGE_CONFIDENCE` (0.80) |
| `bbox_clamped` | quality, per page | warning | a line box had to be clamped into [0, 1] (`details.clamped_count`) |
| `field_<name>` | quality, per extracted field | warning if invalid, info if missing | value invalid or missing |

Quality checks never fail a document. The POST response's `quality_flags` lists the failed
checks of severity `error` and `warning`; `info` rows are kept but do not flag the document.

### Quarantine

`v_quarantine` lists every failed document, and every document with a failed check of
severity `error` or `warning`. Definition: [`docs/quarantine.sql`](docs/quarantine.sql).

```bash
docker compose exec db psql -U docs -d docs -c "select upload_filename, status, error_code, document_type, country_code, failed_checks from v_quarantine"
```

[`docs/quarantine.txt`](docs/quarantine.txt) shows the output after ingesting the samples
plus the four bad files in `tests/fixtures/`: three failed documents (one per error code)
and one accepted but flagged document (low page confidence). None of the samples are
quarantined.

## 9. PII controls

The samples are synthetic, but the pipeline is designed for real claim documents. The
controls in code are:

1. **Log masking** (`app/security/pii.py`). A logging filter is attached to every handler
   of the root logger and of uvicorn's loggers. It masks:
   - NRIC/FIN-like ids (`[STFG]\d{7}[A-Z]`);
   - runs of 9–12 digits (Vietnamese and Philippine id-like numbers);
   - the value after a date-of-birth label;
   - any value tagged as a claimant field (`claimant_<x>=…`, or `extra={"claimant_<x>": …}`).

   The message and any traceback are masked before they reach a handler.
2. **De-identified view** (`v_documents_deidentified`). It shows document metadata and the
   non-identifying extracted fields (amounts, dates, `mc_days`, `provider_name`). It leaves
   out `claimant_name`, `claimant_address`, `claimant_date_of_birth`, raw OCR values and
   upload filenames. Analysts can be given access to this view instead of the tables.
3. **No PII in error text.** Validation messages and API errors never repeat the input
   value. `invalid_request` details list only the field location and the problem.
   `error_message` on a failed document describes the problem, not the content.

Not covered in code (described in the report):
- encryption at rest of `./data` and the database volume;
- database roles that limit raw tables to the pipeline;
- retention and deletion rules for the raw zone;
- masking of the OCR text stored in `ocr_lines` and `chunks`. That text is needed for
  search, so it is stored as it was read.

## 10. Known limitations

- `chunk_embeddings.embedding` is `vector(384)`. A model with another dimension needs a new
  migration (both configured models are 384-dimensional), and `reembed` refuses such a model.
- Numeric dates are read day-first (`05/09/2026` is 5 September), as written in SG, MY and
  VN. Philippine documents usually write month-first: a PH date whose day is ≤ 12 is read
  wrongly, and one whose day is > 12 is marked `invalid`. Two-digit years are `invalid`.
- `signature_presence` is a keyword check ("Signature" or "[signed]"). When neither is
  found the field is `missing`, not `false`, because a signature that is only an image
  cannot be detected.
- Extraction uses regular expressions over line text. It expects labelled values
  (`Label:` followed by the value on the same or next line), and tables or free-text
  values are only partly covered. Its accuracy has been checked only on the three samples.
- Classification is by keywords. A document type that uses none of the keywords is
  rejected as `unsupported_document_type`.
- Filtered search relies on pgvector's iterative HNSW scan (`relaxed_order`). Very
  selective filters over a large corpus can still return fewer than `top_k` results
  (limit: `hnsw.max_scan_tuples`).
- A document ingested while `reembed` is running is embedded with the model that was active
  when it arrived. If the active model switches afterwards, run `reembed` again; it embeds
  only what is missing.
- An API upload embeds its own chunks in one call. Batching across documents happens only
  in the CLI `ingest` and `reembed` commands.
- `CODE_VERSION` is the sha of the checked-out commit and does not reflect uncommitted
  changes.
- The api image is about 2 GB (CPU-only torch and the default model).
- `./data` is created by Docker as root. For a non-Docker run, `sudo chown -R $USER data`
  first.
