-- v_quarantine: documents that need attention.
-- A document is listed when it failed (status = 'failed': error_code says why) or when
-- any quality check of severity error/warning did not pass (failed_checks lists them).
-- Checks of severity info (a field the document does not contain) do not quarantine.
-- Defined in migrations/002_info_severity_and_country_error.sql (first version in 001).

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

-- Usage (output for the samples + tests/fixtures/ is in docs/quarantine.txt):
select upload_filename, status, error_code, document_type, country_code, failed_checks
  from v_quarantine
 order by upload_filename;

-- Details of one quarantined document's failed checks:
-- select check_name, severity, details from quality_checks
--  where document_id = '<document_id>' and not passed;
