"""OCR export parsing and normalisation.

Errors carry the API error code they map to (CLAUDE.md §4, §9).
"""

import functools
import math
import re

from pydantic import ValidationError

from app.errors import PipelineInputError

NORMALISER_VERSION = "1.1.0"


class OcrInputError(PipelineInputError):
    """`check` names the receive-time quality check that failed (CLAUDE.md §3.7)."""

    check: str = "envelope_valid"

    def __init__(self, message: str, check: str | None = None):
        super().__init__(message)
        if check is not None:
            self.check = check


class UnreadableFileError(OcrInputError):
    """The uploaded bytes are not a JSON document."""

    error_code = "unreadable_file"
    check = "file_json"


class InvalidCountryCodeError(OcrInputError):
    """country_code (form field or envelope source.country_code) is not two letters."""

    error_code = "invalid_country_code"


class UnsupportedFormatError(OcrInputError):
    """JSON that is not a supported OCR export (envelope, engine or raw_output shape)."""

    error_code = "unsupported_ocr_format"


def normalise_country_code(value: object) -> str | None:
    """Upper-cased two-letter code; None when absent or blank; else InvalidCountryCodeError.
    Used for both the upload form field and the envelope's source.country_code."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{2}", value.strip()):
        return value.strip().upper()
    raise InvalidCountryCodeError("country_code must be two letters", check="envelope_valid")


def as_number(value, what: str) -> float:
    """A finite JSON number (not bool, not string) as float, else UnsupportedFormatError.
    The offending value is not echoed: it may be OCR text (PII)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise UnsupportedFormatError(f"{what} must be a finite number, got {type(value).__name__}")
    return float(value)


def describe_validation_error(exc: ValidationError) -> str:
    """Field locations and messages only, without the input values (may hold PII)."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    )


def raises_unsupported_format(engine: str):
    """Decorator for normalisers: a malformed raw_output (missing key, wrong type,
    value outside the normalised schema) becomes a named UnsupportedFormatError
    instead of leaking KeyError/TypeError/ValidationError."""

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except UnsupportedFormatError:
                raise
            except ValidationError as exc:
                raise UnsupportedFormatError(
                    f"{engine} raw_output does not fit the normalised schema: "
                    f"{describe_validation_error(exc)}"
                ) from exc
            except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
                raise UnsupportedFormatError(
                    f"{engine} raw_output is malformed: {type(exc).__name__}: {exc}"
                ) from exc

        return wrapper

    return decorate
