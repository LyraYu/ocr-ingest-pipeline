"""Parse and validate the export envelope: {source, ocr{engine, ...}, raw_output}.

Not JSON → UnreadableFileError (`unreadable_file`). JSON without the envelope
keys, an unknown engine, or a raw_output failing that engine's shape check →
UnsupportedFormatError (`unsupported_ocr_format`).
"""

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError

from app.ocr import (
    InvalidCountryCodeError,
    UnreadableFileError,
    UnsupportedFormatError,
    describe_validation_error,
    normalise_country_code,
)
from app.ocr.registry import get_engine
from app.ocr.schema import SourceInfo


class OcrInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    engine: str
    engine_version: str | None = None
    processed_at: datetime | None = None


class Envelope(BaseModel):
    """Envelope keys we rely on; extra top-level keys (export_format_version, ...) are ignored."""

    model_config = ConfigDict(extra="ignore")

    source: SourceInfo
    ocr: OcrInfo
    raw_output: dict

    # Set when source.country_code was present but malformed (it is then None in
    # `source`). Private: cannot be set from the uploaded JSON.
    _source_country_code_invalid: bool = PrivateAttr(default=False)

    @property
    def source_country_code_invalid(self) -> bool:
        return self._source_country_code_invalid


def parse_json(file_bytes: bytes) -> object:
    try:
        return json.loads(file_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnreadableFileError(f"file is not valid JSON: {exc}") from exc


def _normalise_source(source: object) -> tuple[object, bool]:
    """country_code → upper-case two letters or None (blank). A malformed code becomes
    None and is reported (second value) instead of failing the envelope: whether it
    rejects the upload depends on the form field (stages.stage_receive)."""
    if isinstance(source, dict) and "country_code" in source:
        try:
            return {**source, "country_code": normalise_country_code(source["country_code"])}, False
        except InvalidCountryCodeError:
            return {**source, "country_code": None}, True
    return source, False


def validate_envelope(data: object) -> Envelope:
    if not isinstance(data, dict):
        raise UnsupportedFormatError("export must be a JSON object")
    missing = [k for k in ("source", "ocr", "raw_output") if k not in data]
    if not missing and not (isinstance(data["ocr"], dict) and "engine" in data["ocr"]):
        missing.append("ocr.engine")
    if missing:
        raise UnsupportedFormatError(f"envelope is missing {missing}")

    # Source fields are lenient: unknown keys in `source` are dropped, not rejected.
    source, country_invalid = _normalise_source(data["source"])
    if isinstance(source, dict):
        source = {k: v for k, v in source.items() if k in SourceInfo.model_fields}
    try:
        envelope = Envelope.model_validate({**data, "source": source})
    except ValidationError as exc:
        raise UnsupportedFormatError(f"envelope is invalid: {describe_validation_error(exc)}") from exc

    # Unknown engine name, or raw_output not in the declared engine's shape.
    try:
        get_engine(envelope.ocr.engine).check_shape(envelope.raw_output)
    except UnsupportedFormatError as exc:
        raise UnsupportedFormatError(str(exc), check="engine_supported") from exc
    envelope._source_country_code_invalid = country_invalid
    return envelope


def parse_envelope(file_bytes: bytes) -> Envelope:
    return validate_envelope(parse_json(file_bytes))
