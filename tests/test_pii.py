import logging

from app.security.pii import MASK, PiiMaskingFilter, mask


def test_masks_ids_dates_of_birth_and_claimant_fields():
    text = (
        "patient S0000001A / F1234567N, PH id 123456789012, VN id 001099012345, "
        "DOB: 14/03/1988, date of birth 22-11-1992, claimant_name='TAN WEI MING', "
        "claimant_address=Blk-123"
    )
    masked = mask(text)
    for secret in ("S0000001A", "F1234567N", "123456789012", "001099012345", "14/03/1988",
                   "22-11-1992", "TAN WEI MING", "Blk-123"):
        assert secret not in masked
    assert masked.count(MASK) == 8


def test_leaves_ordinary_values_alone():
    text = "document 3f2a run 42 took 1234 ms; total 9320; date_of_mc 02/09/2026; phone 60000000"
    assert mask(text) == text


def _emit(logger: logging.Logger, *args, **kwargs) -> str:
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture()
    handler.addFilter(PiiMaskingFilter())
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error(*args, **kwargs)
    finally:
        logger.removeHandler(handler)
    return records[0]


def test_filter_masks_args_extras_and_tracebacks():
    log = logging.getLogger("tests.pii")
    out = _emit(log, "claimant %s has NRIC %s", "JANE DOE", "T1234567J", extra={"claimant_name": "JANE DOE"})
    assert "JANE DOE" not in out and "T1234567J" not in out

    try:
        raise ValueError("bad id S7654321Z")
    except ValueError:
        out = _emit(log, "failed", exc_info=True)
    assert "S7654321Z" not in out and "ValueError" in out


def test_child_logger_records_are_masked_by_handler_filter():
    """A filter on a handler (as install() does) also sees records from child loggers."""
    parent = logging.getLogger("tests.pii.parent")
    child = logging.getLogger("tests.pii.parent.child")
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Capture()
    handler.addFilter(PiiMaskingFilter())
    parent.addHandler(handler)
    parent.propagate = False
    try:
        child.warning("NRIC S0000002B")
    finally:
        parent.removeHandler(handler)
    assert records == [f"NRIC {MASK}"]
