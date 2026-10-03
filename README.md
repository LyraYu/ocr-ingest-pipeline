# Document Ingestion & Embedding Pipeline

OCR engine exports (AWS Textract, Tesseract, Azure Document Intelligence) →
normalised OCR JSON → PostgreSQL → chunks + pgvector embeddings → search API.
The design spec is `CLAUDE.md`.

Pipeline per document: receive → normalise → load → extract → quality → chunk → embed.
A successful document ends with `status = embedded`.

## Run with Docker

```bash
docker compose up -d db                          # Postgres 16 + pgvector on :5432 (docs/docs/docs)
docker compose build api                         # bakes in the default embedding model
docker compose run --rm api python -m app.cli migrate   # apply migrations/*.sql
docker compose up -d api                         # runs migrate, then uvicorn on :8000
curl localhost:8000/health
```

The image installs CPU-only torch and pre-downloads `sentence-transformers/all-MiniLM-L6-v2`,
so `docker compose up` needs no network. The Hugging Face cache lives in the named volume
`hf-cache` (seeded from the image on first use); other models download into it on demand.

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

# Semantic search over chunks, with the active embedding model
curl -s localhost:8000/search -H 'content-type: application/json' -d '{
  "query": "medical leave for acute gastroenteritis",
  "top_k": 5,
  "filters": {"document_type": "medical_certificate", "country_code": "SG"}
}'
# → {"results": [{"chunk_id", "document_id", "document_type", "page", "score",
#                 "text", "bbox": [x0, y0, x1, y1], "embedding_model"}]}
```

Search: `top_k` is an integer 1–50 (default 5). `filters` is optional:
- `document_type` must be one of `referral_letter`, `medical_certificate`, `receipt`.
- `country_code` is two letters, case-insensitive.

`score` is `1 - cosine distance`, so higher means more similar. Only `chunk_embeddings`
rows of the active model are searched.

Errors use the body `{"error": "<code>"}`:

| HTTP | code | when |
|---|---|---|
| 400 | `file_missing` | no `file` part, or an empty file |
| 422 | `unreadable_file` | not JSON (still stored in the raw zone, `documents` row with `status = failed`) |
| 422 | `unsupported_ocr_format` | no envelope, unknown `ocr.engine`, or `raw_output` not in that engine's shape (stored + failed row) |
| 422 | `unsupported_document_type` | no document-type keyword in the text (stored + failed row) |
| 422 | `invalid_country_code` | `country_code` form field, or the envelope's `source.country_code`, is not two letters (envelope case: stored + failed row) |
| 422 | `invalid_request` | request body/parameters fail validation (any endpoint); `detail` lists the problems |
| 404 | `not_found` | `GET /documents/{id}` for an unknown or malformed id |
| 500 | `internal_server_error` | unexpected failure; the document is marked failed |

Re-uploading a file that was rejected returns the same error again (no new rows).

## CLI

```bash
# Ingest every file in a folder (non-recursive) under one pipeline_runs row.
docker compose run --rm api python -m app.cli ingest samples
# Any host folder: mount it into the container.
docker compose run --rm -v /path/to/exports:/in:ro api python -m app.cli ingest /in --country-code SG

# Re-embed every embedded document with another model, then switch search to it.
docker compose run --rm api python -m app.cli reembed --model BAAI/bge-small-en-v1.5
```

`ingest` runs each file through receive → chunk, then embeds all new chunks of the batch in
one encode call. It prints one line per file (`processed` / `duplicate` /
`failed <file>: <error_code>`), then `processed: N  duplicate: N  failed: N`. The run's
`stats` column holds the same counts.

`reembed` prints the number of documents, re-embedded documents and failures, the new
model's embedding count, and the active model. It exits with status 1 when any document
failed; in that case the active model is left unchanged.

## How to switch the embedding model and re-embed

Embeddings live in `chunk_embeddings`, keyed by `(chunk_id, model_name, model_version)`,
so several models' vectors coexist for the same chunk. `embedding_models` has exactly one
row with `is_active = true`; search embeds the query with that model and reads only that
model's rows.

1. Run `python -m app.cli reembed --model <hub id or local path>`. The model must produce
   384-dimensional vectors (see Known limitations).
   - It is loaded from the HF cache, or downloaded if absent.
   - `model_version` is the hub commit of the loaded snapshot.
2. One `pipeline_runs` row with `run_type = reembed` covers the whole operation. For every
   document with `status = embedded`:
   - `stage_chunk` reuses the existing chunks of the current `CHUNKING_VERSION`.
   - `stage_embed` adds a row for each chunk that has none for this model+version.

   All chunks are encoded in one call. Rows of other models are never touched, and document
   status stays `embedded` throughout.
3. Only if every document succeeded, `is_active` flips to the new model. Until then search
   keeps using the old model's rows. If anything failed, nothing flips: re-running reembed
   embeds only the missing chunks.
4. To switch back, run `reembed --model <old model>`. Its rows still exist, so nothing is
   re-encoded and only the active flag flips.

New uploads are embedded with the active model. `EMBEDDING_MODEL` (config) is used only
before any model is active, i.e. for the very first ingest.

To drop an old model's vectors once they are no longer needed:

```sql
delete from chunk_embeddings where model_name = 'sentence-transformers/all-MiniLM-L6-v2';
delete from embedding_models where model_name = 'sentence-transformers/all-MiniLM-L6-v2' and not is_active;
```

## Chunking

`app/chunking.py`, `CHUNKING_VERSION = "1.0"`. Chunks are built from whole lines, page by
page (never across pages), up to 400 characters (lines joined with `\n`). The last line of a
chunk is repeated as the first line of the next, which keeps a `Label:` line next to its
value. A single line longer than 400 characters becomes its own chunk. Each chunk stores
its ordered `source_line_ids` and the union bbox of those lines.

## Extraction and quality

- `app/extraction/classify.py` picks `document_type` from weighted keywords (highest total
  weight wins; a tie goes to the earliest match). No keyword → `unsupported_document_type`.
- `app/extraction/rules.py` holds one `field_name → (regex, value_type)` dict per type. Its
  keys are the CLAUDE.md §6.1 field list.
  - Every field gets an `extracted_fields` row (`valid` / `invalid` / `missing`), with the
    ids of the `ocr_lines` the match came from.
  - `provider_name` falls back to the first line of page 1, skipping lines that mention
    Fullerton Health.
- `app/extraction/validate.py` normalises values: dates → `DD/MM/YYYY`, `*_date_time` →
  `DD/MM/YYYY HH:MM`, amounts → integer in the smallest currency unit (below), `mc_days` →
  non-negative int.

### Amounts

Every amount is stored as an integer in the currency's smallest unit: currency symbols,
thousands separators and the decimal point are removed. An amount without decimals is
scaled the same way, so all values of one currency share a unit:

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
| `VND`, `₫` | 0 (no minor unit; not scaled) |

Any other currency, a negative amount, or more decimals than the currency has → `invalid`.
The table is `CURRENCY_EXPONENT` in `app/extraction/validate.py`.

### Quality checks

A `quality_checks` row means the check ran.

| check | written by | severity | fails when |
|---|---|---|---|
| `file_json`, `envelope_valid`, `engine_supported` | receive (in this order; checks after a failure did not run and have no row) | error | file not JSON / envelope keys or country code invalid / unknown engine or raw_output shape |
| `document_type_supported` | extract | error | no type keyword |
| `page_confidence` | quality, per page + one document-level row | warning | mean line confidence < `QUALITY_MIN_PAGE_CONFIDENCE` (0.80) |
| `bbox_clamped` | quality, per page | warning | any line box had to be clamped into [0, 1] (`details.clamped_count`) |
| `field_<name>` | quality, per extracted field | warning if invalid, info if missing | value invalid or missing |

Quality checks never fail a document. In the POST response, `quality_flags` lists the
failed checks of severity `error` or `warning`; `info` rows are recorded but don't flag.

### Quarantine

`v_quarantine` lists failed documents, plus any document with a failed `error` or `warning`
check:

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
   - The keys are the type's field list.
   - Build the regexes with `labelled(labels, VALUE)` and the value patterns `TEXT`, `DATE`,
     `DATETIME`, `AMOUNT`, `NUMBER`; reuse the shared `CLAIMANT_*` / `PROVIDER_NAME` rules.
   - A new value_type needs a parser in `PARSERS_BY_TYPE` in `app/extraction/validate.py`;
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
5. **`app/api/search.py`**: add the type to the `document_type` filter's `Literal`.
6. **Tests**: a sample export in `tests/`, plus a case in `tests/test_extraction.py`.

No table changes are needed: `extracted_fields` is key-value.

## Normalisation

`app/ocr/` turns each engine's `raw_output` into one schema (`app/ocr/schema.py`):
- bboxes are `[x0, y0, x1, y1]` as fractions of the page, origin top-left;
- confidences are in [0, 1];
- `NORMALISER_VERSION = "1.1.0"`.

| engine | page size | line confidence |
|---|---|---|
| aws-textract | null (`size_reason` explains: ratio coordinates only) | LINE `Confidence / 100` |
| tesseract | level-1 row, px | mean of word `conf` (ignoring -1) / 100 |
| azure-document-intelligence | page `width`/`height`, inch (or px) | mean of words whose span lies in the line's span |

Bbox coordinates slightly off the page (skewed scans) are clamped into [0, 1], and the
line gets `bbox_clamped: true`. Only a box that is inverted after clamping is rejected.
Textract LINE blocks without `Page` get their page from the PAGE block that lists them as
a CHILD.

Errors: non-JSON → `unreadable_file`; missing envelope keys, unknown engine, or a
raw_output that does not match the declared engine → `unsupported_ocr_format`.

### Normaliser versions

`NORMALISER_VERSION` (in `app/ocr/__init__.py`) must change whenever the normalised JSON
changes: its shape, or the values a given input produces. The version is part of the
normalised file name (`data/normalised/<sha256>.v<version>.json`) and is recorded on every
`pipeline_runs` row. Old and new files therefore sit side by side, and every row in the
database can be traced back to the normaliser that produced it.

Worked example, **1.0.0 → 1.1.0**: lines gained the field `bbox_clamped` (true when a
coordinate was clamped into [0, 1]), which feeds the `bbox_clamped` quality check. That is
an additive schema change, so it is a minor bump. Documents normalised by 1.0.0 keep their
`.v1.0.0.json` files. To bring them to 1.1.0, re-run `stage_normalise` and then the later
stages for those documents; this writes a `.v1.1.0.json` file next to the old one.

## How to add support for a new OCR engine format

Example: Google Document AI, engine name `google-document-ai`.

1. **`app/ocr/google_docai.py`**: two pure functions, following `textract.py`:
   - `check_shape(raw_output: dict) -> None`: a cheap structural check that raises
     `UnsupportedFormatError` when `raw_output` is not this engine's shape.
   - `normalise(raw_output: dict) -> list[NormalisedPage]`, decorated with
     `@raises_unsupported_format(ENGINE)`. Each page has 1-based `page_number`,
     `width`/`height`/`size_unit` (or all null plus a `size_reason`), and `lines` in reading
     order. Each line has `line_index` 0..n-1, `text`, `bbox` as page fractions
     `[x0, y0, x1, y1]` (out-of-range values are clamped by the schema), `confidence` in
     [0, 1] or null, and `confidence_source` (`engine_line` or `mean_of_words`).

   Use `as_number()` for numeric input, so bad values become named errors.
2. **`app/ocr/registry.py`**: add `google_docai.ENGINE: EngineAdapter(google_docai.check_shape, google_docai.normalise)`
   to `ENGINES`. Envelope validation accepts every name in `ENGINES`.
3. **`app/ocr/schema.py`**: add the name to the `EngineName` literal.
4. **`NORMALISER_VERSION`**: bump the minor version. Existing outputs are unchanged, but
   the normaliser now accepts more input.
5. **Tests** in `tests/test_normalisers.py`:
   - a sample export in `samples/`, which the fixtures key by `ocr.engine`;
   - a `test_normalise_<engine>` with the page/line/bbox/confidence assertions;
   - a branch in `test_malformed_raw_output_is_named_error`.

   `test_wrong_engine_raw_output_is_rejected` is parametrised over `ENGINES` and picks up
   the new engine automatically.
6. **CLAUDE.md §4**: document the per-engine rules.

Nothing downstream changes: `load` and every later stage read only the normalised JSON.

## Storage zones

- Raw zone: `data/raw/<sha256>.json`, the uploaded bytes, for every non-empty upload.
- Normalised zone: `data/normalised/<sha256>.v<normaliser_version>.json`. The `load`
  stage reads only this file, never the raw one.
- `./data` is mounted into the api container. Docker creates it as root; for a local
  (non-docker) run, `sudo chown -R $USER data` first.

## Tests

Tests run against the compose Postgres. Each DB test module truncates the data tables, and
raw/normalised files go to a temp dir, not `./data`. The reembed test downloads
`BAAI/bge-small-en-v1.5` (~130 MB) into the `hf-cache` volume on its first run.

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

## Known limitations

- `chunk_embeddings.embedding` is `vector(384)`; a model with another dimension needs a new
  migration. Both configured models are 384-dimensional.
- Numeric dates are read day-first (`05/09/2026` = 5 September), as written in SG, MY and
  VN. Philippine documents usually write month-first, so a PH date whose day is ≤ 12 is
  read wrongly, and one whose day is > 12 is marked `invalid`. Two-digit years are marked
  `invalid`.
- `signature_presence` is a keyword heuristic ("Signature" / "[signed]"). When neither is
  found the field is `missing`, not `false`: an image-only signature cannot be detected.
- Filtered search relies on pgvector's iterative HNSW scan (`hnsw.iterative_scan =
  relaxed_order`, pgvector ≥ 0.8). Very selective filters over a large corpus may still
  return fewer than `top_k` rows, bounded by `hnsw.max_scan_tuples`.
- A document ingested while a `reembed` is running is embedded with the model that was
  active when it arrived. If the flip happens afterwards, run `reembed` again for the new
  model (it only embeds what is missing).
