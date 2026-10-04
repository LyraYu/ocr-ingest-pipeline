-- 001_initial: full relational schema from docs/DESIGN.md section 3.
-- Conventions: uuid ids via gen_random_uuid(), timestamptz default now(),
-- bboxes [x0, y0, x1, y1] as fractions of the page in [0, 1], confidences in [0, 1].

create extension if not exists vector;
create extension if not exists pgcrypto;  -- gen_random_uuid() is core in pg13+, kept for portability

-- 3.1 pipeline_runs ---------------------------------------------------------
create table pipeline_runs (
    id                      uuid primary key default gen_random_uuid(),
    run_type                text not null check (run_type in ('ingest', 'reembed', 'renormalise')),
    started_at              timestamptz not null default now(),
    finished_at             timestamptz,
    status                  text not null default 'running'
                            check (status in ('running', 'succeeded', 'failed', 'partial')),
    code_version            text not null default 'unknown',
    normaliser_version      text not null,
    embedding_model         text,
    embedding_model_version text,
    stats                   jsonb not null default '{}'::jsonb
);

-- 3.2 documents -------------------------------------------------------------
create table documents (
    id                     uuid primary key default gen_random_uuid(),
    content_hash           text not null unique,
    source_sha256          text,
    upload_filename        text,
    source_filename        text,
    source_mime_type       text,
    size_bytes             integer not null check (size_bytes >= 0),
    raw_storage_uri        text not null,
    normalised_storage_uri text,
    document_type          text check (document_type in ('referral_letter', 'medical_certificate', 'receipt')),
    country_code           char(2) not null,
    ocr_engine             text,
    ocr_engine_version     text,
    ocr_processed_at       timestamptz,
    status                 text not null
                           check (status in ('received', 'normalised', 'loaded', 'extracted', 'embedded', 'failed')),
    error_code             text check (error_code in ('file_missing', 'unreadable_file', 'unsupported_ocr_format',
                                                      'unsupported_document_type', 'internal_server_error')),
    error_message          text,
    ingested_at            timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    latest_run_id          uuid references pipeline_runs (id),
    constraint documents_error_code_only_when_failed check (status = 'failed' or error_code is null)
);

-- Not unique: a re-OCR of the same scan is a new document sharing this value.
create index documents_source_sha256_idx on documents (source_sha256);
create index documents_status_idx on documents (status);

-- 3.3 document_stages -------------------------------------------------------
create table document_stages (
    id          uuid primary key default gen_random_uuid(),
    document_id uuid not null references documents (id) on delete cascade,
    stage       text not null
                check (stage in ('receive', 'normalise', 'load', 'extract', 'quality', 'chunk', 'embed')),
    status      text not null check (status in ('succeeded', 'failed', 'skipped')),
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    duration_ms integer check (duration_ms >= 0),
    error       text,
    run_id      uuid not null references pipeline_runs (id),
    unique (document_id, stage, run_id)
);

-- 3.4 document_pages --------------------------------------------------------
create table document_pages (
    id                 uuid primary key default gen_random_uuid(),
    document_id        uuid not null references documents (id) on delete cascade,
    page_number        integer not null check (page_number >= 1),
    width              numeric check (width > 0),
    height             numeric check (height > 0),
    size_unit          text check (size_unit in ('px', 'inch')),
    size_reason        text,
    ocr_engine         text not null,
    ocr_engine_version text,
    mean_confidence    numeric check (mean_confidence between 0 and 1),
    line_count         integer not null check (line_count >= 0),
    run_id             uuid not null references pipeline_runs (id),
    unique (document_id, page_number)
);

-- 3.5 ocr_lines -------------------------------------------------------------
create table ocr_lines (
    id                uuid primary key default gen_random_uuid(),
    document_id       uuid not null references documents (id) on delete cascade,
    page_id           uuid not null references document_pages (id) on delete cascade,
    line_index        integer not null check (line_index >= 0),
    text              text not null,
    bbox_x0           numeric not null check (bbox_x0 between 0 and 1),
    bbox_y0           numeric not null check (bbox_y0 between 0 and 1),
    bbox_x1           numeric not null check (bbox_x1 between 0 and 1),
    bbox_y1           numeric not null check (bbox_y1 between 0 and 1),
    confidence        numeric check (confidence between 0 and 1),
    confidence_source text check (confidence_source in ('engine_line', 'mean_of_words')),
    run_id            uuid not null references pipeline_runs (id),
    unique (page_id, line_index)
);

create index ocr_lines_document_id_idx on ocr_lines (document_id);

-- 3.6 extracted_fields ------------------------------------------------------
create table extracted_fields (
    id                 uuid primary key default gen_random_uuid(),
    document_id        uuid not null references documents (id) on delete cascade,
    field_name         text not null,
    raw_value          text,
    normalised_value   jsonb,
    value_type         text not null check (value_type in ('date', 'amount', 'int', 'bool', 'text', 'datetime')),
    validation_status  text not null check (validation_status in ('valid', 'invalid', 'missing')),
    validation_message text,
    source_line_ids    uuid[] not null default '{}',
    run_id             uuid not null references pipeline_runs (id),
    unique (document_id, field_name)
);

-- 3.7 quality_checks --------------------------------------------------------
create table quality_checks (
    id          uuid primary key default gen_random_uuid(),
    document_id uuid not null references documents (id) on delete cascade,
    page_id     uuid references document_pages (id) on delete cascade,
    check_name  text not null,
    passed      boolean not null,
    severity    text not null check (severity in ('error', 'warning')),
    details     jsonb not null default '{}'::jsonb,
    run_id      uuid not null references pipeline_runs (id)
);

create index quality_checks_document_id_idx on quality_checks (document_id);

-- 3.8 chunks ----------------------------------------------------------------
create table chunks (
    id               uuid primary key default gen_random_uuid(),
    document_id      uuid not null references documents (id) on delete cascade,
    page_id          uuid not null references document_pages (id) on delete cascade,
    chunk_index      integer not null check (chunk_index >= 0),
    chunking_version text not null,
    text             text not null,
    bbox_x0          numeric not null check (bbox_x0 between 0 and 1),
    bbox_y0          numeric not null check (bbox_y0 between 0 and 1),
    bbox_x1          numeric not null check (bbox_x1 between 0 and 1),
    bbox_y1          numeric not null check (bbox_y1 between 0 and 1),
    source_line_ids  uuid[] not null,
    char_count       integer not null check (char_count >= 0),
    run_id           uuid not null references pipeline_runs (id),
    unique (document_id, chunking_version, chunk_index)
);

-- 3.9 chunk_embeddings ------------------------------------------------------
-- Known limitation: fixed at 384 dims; a model with another dimension needs a new migration.
create table chunk_embeddings (
    id            uuid primary key default gen_random_uuid(),
    chunk_id      uuid not null references chunks (id) on delete cascade,
    model_name    text not null,
    model_version text not null default 'unknown',
    dimension     integer not null,
    embedding     vector(384) not null,
    created_at    timestamptz not null default now(),
    run_id        uuid not null references pipeline_runs (id),
    unique (chunk_id, model_name, model_version)
);

-- Distance metric: cosine. Search score = 1 - cosine_distance.
create index chunk_embeddings_embedding_hnsw_idx
    on chunk_embeddings using hnsw (embedding vector_cosine_ops);
create index chunk_embeddings_model_idx on chunk_embeddings (model_name, model_version);

-- 3.10 embedding_models -----------------------------------------------------
create table embedding_models (
    model_name    text primary key,
    model_version text not null default 'unknown',
    dimension     integer not null,
    is_active     boolean not null default false,
    created_at    timestamptz not null default now()
);

-- At most one active model (the reembed flip keeps it at exactly one).
create unique index embedding_models_single_active_idx on embedding_models (is_active) where is_active;

-- 3.11 v_quarantine ---------------------------------------------------------
create view v_quarantine as
select
    d.id            as document_id,
    d.upload_filename,
    d.status,
    d.error_code,
    d.error_message,
    d.document_type,
    d.country_code,
    d.ocr_engine,
    coalesce(
        array_agg(distinct qc.check_name order by qc.check_name) filter (where qc.passed = false),
        '{}'::text[]
    )               as failed_checks,
    d.ingested_at
from documents d
left join quality_checks qc on qc.document_id = d.id
group by d.id
having d.status = 'failed' or bool_or(qc.passed = false);

-- 3.12 v_documents_deidentified (PII control #2) ----------------------------
-- Document metadata plus non-identifying extracted fields. Claimant fields are
-- excluded; filenames are left out because they can carry a claimant's name.
create view v_documents_deidentified as
select
    d.id            as document_id,
    d.document_type,
    d.country_code,
    d.status,
    d.ocr_engine,
    d.ocr_engine_version,
    d.ingested_at,
    f.field_name,
    f.normalised_value,
    f.value_type,
    f.validation_status
from documents d
left join extracted_fields f
       on f.document_id = d.id
      and f.field_name not in ('claimant_name', 'claimant_address', 'claimant_date_of_birth');
