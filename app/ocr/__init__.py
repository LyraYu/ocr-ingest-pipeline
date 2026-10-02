"""OCR export parsing and normalisation.

Errors carry the API error code they map to (CLAUDE.md §4, §9).
"""

import functools
import math

from pydantic import ValidationError

NORMALISER_VERSION = "1.0.0"


class OcrInputError(Exception):
    error_code: str = "internal_server_error"


class UnreadableFileError(OcrInputError):
    """The uploaded bytes are not a JSON document."""

    error_code = "unreadable_file"


class UnsupportedFormatError(OcrInputError):
    """JSON that is not a supported OCR export (envelope, engine or raw_output shape)."""

    error_code = "unsupported_ocr_format"


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
