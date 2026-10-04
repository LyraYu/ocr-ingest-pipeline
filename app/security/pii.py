"""Log masking (docs/DESIGN.md §8, PII control #1).

`PiiMaskingFilter` rewrites every log record before it is emitted:
- NRIC/FIN-like ids (`[STFG]\\d{7}[A-Z]`)
- digit runs of 9–12 digits (Vietnamese / Philippine national-id-like numbers)
- dates of birth: the value after a DOB label ("DOB:", "Date of Birth", "claimant_date_of_birth=")
- any value tagged as a claimant field: `claimant_<x>=value` / `claimant_<x>: value`
  in the message, and `extra={"claimant_<x>": ...}` attributes (masked in place,
  and wherever their value appears in the message).

The message is formatted (msg % args) before masking, and a traceback is formatted
and masked here too, so neither reaches a handler unmasked.

`install()` attaches the filter to the handlers of the root logger and of uvicorn's
loggers. A filter on a logger only sees records logged on that logger itself; a
filter on a handler sees every record the handler emits, including those
propagated from child loggers.
"""

import logging
import re

MASK = "[REDACTED]"

NRIC_FIN = re.compile(r"\b[STFG]\d{7}[A-Z]\b")
LONG_DIGIT_RUN = re.compile(r"(?<!\d)\d{9,12}(?!\d)")
DOB_VALUE = re.compile(
    r"(?i)(\b(?:date\s+of\s+birth|birth\s*date|d\.?o\.?b\.?|claimant_date_of_birth)\b\s*[:=]?\s*)"
    r"('[^']*'|\"[^\"]*\"|[^\s,;]+)"
)
CLAIMANT_VALUE = re.compile(r"(?i)(\bclaimant_[a-z_]+\b\s*[:=]\s*)('[^']*'|\"[^\"]*\"|[^\s,;]+)")


def mask(text: str) -> str:
    text = DOB_VALUE.sub(lambda m: m.group(1) + MASK, text)
    text = CLAIMANT_VALUE.sub(lambda m: m.group(1) + MASK, text)
    text = NRIC_FIN.sub(MASK, text)
    return LONG_DIGIT_RUN.sub(MASK, text)


class PiiMaskingFilter(logging.Filter):
    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for key, value in list(vars(record).items()):
            if key.startswith("claimant_") and value is not None:
                if str(value):
                    message = message.replace(str(value), MASK)
                setattr(record, key, MASK)
        record.msg = mask(message)
        record.args = None
        if record.exc_info:
            record.exc_text = mask(self._formatter.formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = mask(record.stack_info)
        return True


_FILTER = PiiMaskingFilter()
LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def install(level: int = logging.INFO) -> None:
    """Idempotent. Ensures the root logger has a handler, then masks on every handler
    of the root and uvicorn loggers."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for name in LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
