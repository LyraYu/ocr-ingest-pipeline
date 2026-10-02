"""Parse and validate the export envelope: {source, ocr{engine, ...}, raw_output}.

Not JSON → UnreadableFileError (`unreadable_file`). JSON without the envelope
keys, an unknown engine, or a raw_output failing that engine's shape check →
UnsupportedFormatError (`unsupported_ocr_format`).
"""

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from app.ocr import UnreadableFileError, UnsupportedFormatError
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


def parse_json(file_bytes: bytes) -> object:
    try:
        return json.loads(file_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnreadableFileError(f"file is not valid JSON: {exc}") from exc


def _normalise_source(source: object) -> object:
    """Upper-case country_code ('sg' → 'SG') before validation; other values pass through."""
    if isinstance(source, dict) and isinstance(source.get("country_code"), str):
        return {**source, "country_code": source["country_code"].strip().upper()}
    return source


def validate_envelope(data: object) -> Envelope:
    if not isinstance(data, dict):
        raise UnsupportedFormatError("export must be a JSON object")
    missing = [k for k in ("source", "ocr", "raw_output") if k not in data]
    if not missing and not (isinstance(data["ocr"], dict) and "engine" in data["ocr"]):
        missing.append("ocr.engine")
    if missing:
        raise UnsupportedFormatError(f"envelope is missing {missing}")

    # Source fields are lenient: unknown keys in `source` are dropped, not rejected.
    source = _normalise_source(data["source"])
    if isinstance(source, dict):
        source = {k: v for k, v in source.items() if k in SourceInfo.model_fields}
    try:
        envelope = Envelope.model_validate({**data, "source": source})
    except ValidationError as exc:
        raise UnsupportedFormatError(f"envelope is invalid: {exc}") from exc

    get_engine(envelope.ocr.engine).check_shape(envelope.raw_output)
    return envelope


def parse_envelope(file_bytes: bytes) -> Envelope:
    return validate_envelope(parse_json(file_bytes))
