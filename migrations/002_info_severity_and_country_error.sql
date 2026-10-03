-- 002: severity `info` for checks that do not quarantine (a missing extracted field),
-- `invalid_country_code` as a document error code (invalid envelope source.country_code),
-- and v_quarantine counting only error/warning checks.

alter table quality_checks drop constraint quality_checks_severity_check;
alter table quality_checks add constraint quality_checks_severity_check
    check (severity in ('error', 'warning', 'info'));

alter table documents drop constraint documents_error_code_check;
alter table documents add constraint documents_error_code_check
    check (error_code in ('file_missing', 'unreadable_file', 'unsupported_ocr_format',
                          'unsupported_document_type', 'invalid_country_code', 'internal_server_error'));

create or replace view v_quarantine as
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
        array_agg(distinct qc.check_name order by qc.check_name)
            filter (where qc.passed = false and qc.severity in ('error', 'warning')),
        '{}'::text[]
    )               as failed_checks,
    d.ingested_at
from documents d
left join quality_checks qc on qc.document_id = d.id
group by d.id
having d.status = 'failed'
    or bool_or(qc.passed = false and qc.severity in ('error', 'warning'));
