import copy
import json

import pytest

from app.ocr import UnreadableFileError, UnsupportedFormatError
from app.ocr.envelope import parse_envelope, validate_envelope


def test_samples_parse(samples_by_engine):
    for engine, data in samples_by_engine.items():
        envelope = parse_envelope(json.dumps(data).encode())
        assert envelope.ocr.engine == engine
        assert envelope.ocr.processed_at is not None


@pytest.mark.parametrize("payload", [b"not json", b"\xff\xfe\x00garbage", b""])
def test_non_json_is_unreadable(payload):
    with pytest.raises(UnreadableFileError):
        parse_envelope(payload)


@pytest.mark.parametrize("payload", [b"[]", b'"x"', b"{}", b'{"source": {}, "ocr": {}, "raw_output": {}}'])
def test_json_without_envelope_is_unsupported(payload):
    with pytest.raises(UnsupportedFormatError):
        parse_envelope(payload)


def test_unknown_engine_is_unsupported(samples_by_engine):
    data = copy.deepcopy(samples_by_engine["tesseract"])
    data["ocr"]["engine"] = "google-vision"
    with pytest.raises(UnsupportedFormatError, match="unknown ocr.engine"):
        validate_envelope(data)


def test_declared_engine_shape_mismatch_is_unsupported(samples_by_engine):
    data = copy.deepcopy(samples_by_engine["tesseract"])
    data["ocr"]["engine"] = "aws-textract"
    with pytest.raises(UnsupportedFormatError):
        validate_envelope(data)
