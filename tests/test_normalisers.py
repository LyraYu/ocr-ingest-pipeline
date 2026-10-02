import copy

import pytest

from app.ocr import UnsupportedFormatError
from app.ocr.registry import ENGINES, normalise_envelope
from app.ocr.envelope import validate_envelope
from app.ocr.schema import NormalisedPage


def _assert_well_formed(pages: list[NormalisedPage], expected_pages: int, expected_lines: int):
    assert len(pages) == expected_pages
    assert sum(len(p.lines) for p in pages) == expected_lines
    for page in pages:
        assert len(page.lines) > 0
        assert [line.line_index for line in page.lines] == list(range(len(page.lines)))
        for line in page.lines:
            assert line.text.strip()
            assert all(0.0 <= v <= 1.0 for v in line.bbox), line
            x0, y0, x1, y1 = line.bbox
            assert x0 <= x1 and y0 <= y1
            assert line.confidence is None or 0.0 <= line.confidence <= 1.0, line


def test_normalise_textract(samples_by_engine):
    raw = samples_by_engine["aws-textract"]["raw_output"]
    pages = ENGINES["aws-textract"].normalise(raw)

    expected_pages = sum(1 for b in raw["Blocks"] if b["BlockType"] == "PAGE")
    expected_lines = sum(1 for b in raw["Blocks"] if b["BlockType"] == "LINE")
    _assert_well_formed(pages, expected_pages, expected_lines)
    for page in pages:
        assert page.width is None and page.height is None and page.size_unit is None
        assert page.size_reason
        assert all(line.confidence_source == "engine_line" for line in page.lines)


def test_normalise_tesseract(samples_by_engine):
    raw = samples_by_engine["tesseract"]["raw_output"]
    pages = ENGINES["tesseract"].normalise(raw)

    expected_pages = raw["level"].count(1)
    expected_lines = len({
        (raw["page_num"][i], raw["block_num"][i], raw["par_num"][i], raw["line_num"][i])
        for i, level in enumerate(raw["level"])
        if level == 5 and raw["text"][i].strip()
    })
    _assert_well_formed(pages, expected_pages, expected_lines)
    for page in pages:
        assert page.size_unit == "px" and page.width > 0 and page.height > 0
        assert all(line.confidence_source == "mean_of_words" for line in page.lines)


def test_normalise_azure(samples_by_engine):
    raw = samples_by_engine["azure-document-intelligence"]["raw_output"]
    pages = ENGINES["azure-document-intelligence"].normalise(raw)

    result_pages = raw["analyzeResult"]["pages"]
    _assert_well_formed(pages, len(result_pages), sum(len(p["lines"]) for p in result_pages))
    for page in pages:
        assert page.size_unit == "inch" and page.width > 0 and page.height > 0
        assert all(line.confidence_source == "mean_of_words" for line in page.lines)


def test_tesseract_line_merges_words_and_ignores_missing_conf():
    raw = {
        "level":     [1,   5,      5,     5],
        "page_num":  [1,   1,      1,     1],
        "block_num": [0,   1,      1,     1],
        "par_num":   [0,   1,      1,     1],
        "line_num":  [0,   1,      1,     1],
        "word_num":  [0,   1,      2,     3],
        "left":      [0,   10,     60,    100],
        "top":       [0,   20,     25,    20],
        "width":     [200, 40,     30,    10],
        "height":    [100, 10,     10,    10],
        "conf":      [-1,  90.0,   -1,    70.0],
        "text":      ["",  "Total", "S$5", " "],
    }
    [page] = ENGINES["tesseract"].normalise(raw)
    [line] = page.lines
    assert line.text == "Total S$5"
    assert line.bbox == pytest.approx((10 / 200, 20 / 100, 90 / 200, 35 / 100))
    assert line.confidence == pytest.approx(0.90)


def test_azure_line_confidence_uses_word_span_inside_line_span():
    raw = {"analyzeResult": {"content": "AB CD\nEF", "pages": [{
        "pageNumber": 1, "width": 10, "height": 20, "unit": "inch",
        "lines": [
            {"content": "AB CD", "polygon": [1, 2, 5, 2, 5, 4, 1, 4], "spans": [{"offset": 0, "length": 5}]},
            {"content": "EF", "polygon": [1, 6, 3, 6, 3, 8, 1, 8], "spans": [{"offset": 6, "length": 2}]},
        ],
        "words": [
            {"content": "AB", "confidence": 0.9, "span": {"offset": 0, "length": 2}},
            {"content": "CD", "confidence": 0.7, "span": {"offset": 3, "length": 2}},
            {"content": "EF", "confidence": 0.5, "span": {"offset": 6, "length": 2}},
        ],
    }]}}
    [page] = ENGINES["azure-document-intelligence"].normalise(raw)
    assert [line.confidence for line in page.lines] == pytest.approx([0.8, 0.5])
    assert page.lines[0].bbox == pytest.approx((0.1, 0.1, 0.5, 0.2))


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_wrong_engine_raw_output_is_rejected(samples_by_engine, engine):
    others = [s["raw_output"] for name, s in samples_by_engine.items() if name != engine]
    assert len(others) == 2
    for raw in others:
        with pytest.raises(UnsupportedFormatError):
            ENGINES[engine].normalise(raw)


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_malformed_raw_output_is_named_error(samples_by_engine, engine):
    """A shape that passes the check but is broken inside → UnsupportedFormatError, not KeyError."""
    raw = copy.deepcopy(samples_by_engine[engine]["raw_output"])
    if engine == "aws-textract":
        next(b for b in raw["Blocks"] if b["BlockType"] == "LINE").pop("Geometry")
    elif engine == "tesseract":
        raw.pop("page_num")
    else:
        raw["analyzeResult"]["pages"][0]["lines"][0].pop("polygon")
    with pytest.raises(UnsupportedFormatError):
        ENGINES[engine].normalise(raw)


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_normalise_envelope_builds_document(samples_by_engine, engine):
    envelope = validate_envelope(samples_by_engine[engine])
    doc = normalise_envelope(envelope)
    assert doc.normaliser_version == "1.0.0"
    assert doc.engine.name == engine
    assert doc.source.country_code == "SG"
    assert doc.pages
    # Round-trips through JSON (this is what gets written to data/normalised/).
    assert type(doc).model_validate_json(doc.model_dump_json()) == doc


def _azure_one_line(polygon: list[float]) -> dict:
    return {"analyzeResult": {"content": "X", "pages": [{
        "pageNumber": 1, "width": 10, "height": 20, "unit": "inch",
        "lines": [{"content": "X", "polygon": polygon, "spans": [{"offset": 0, "length": 1}]}],
        "words": [{"content": "X", "confidence": 0.9, "span": {"offset": 0, "length": 1}}],
    }]}}


def test_bbox_is_clamped_into_unit_square():
    # x from -0.5 to 10.4 inch on a 10-inch page, y from -1 to 21 on a 20-inch page.
    [page] = ENGINES["azure-document-intelligence"].normalise(
        _azure_one_line([-0.5, -1, 10.4, -1, 10.4, 21, -0.5, 21])
    )
    assert page.lines[0].bbox == (0.0, 0.0, 1.0, 1.0)


def test_inverted_bbox_is_rejected_after_clamping(samples_by_engine):
    raw = copy.deepcopy(samples_by_engine["aws-textract"]["raw_output"])
    line = next(b for b in raw["Blocks"] if b["BlockType"] == "LINE")
    line["Geometry"]["BoundingBox"].update(Left=0.5, Width=-0.2)  # x1 = 0.3 < x0 = 0.5
    with pytest.raises(UnsupportedFormatError, match="x0<=x1"):
        ENGINES["aws-textract"].normalise(raw)


def test_textract_page_from_block_tree_when_line_page_missing(samples_by_engine):
    raw = copy.deepcopy(samples_by_engine["aws-textract"]["raw_output"])
    lines = [b for b in raw["Blocks"] if b["BlockType"] == "LINE"]
    for block in lines:
        block.pop("Page")
    [page] = ENGINES["aws-textract"].normalise(raw)
    assert page.page_number == 1
    assert len(page.lines) == len(lines) == 31


def test_textract_page_from_position_when_page_block_has_no_page_either(samples_by_engine):
    raw = copy.deepcopy(samples_by_engine["aws-textract"]["raw_output"])
    for block in raw["Blocks"]:
        block.pop("Page", None)
    [page] = ENGINES["aws-textract"].normalise(raw)
    assert page.page_number == 1 and len(page.lines) == 31


def test_textract_line_without_page_or_parent_is_rejected(samples_by_engine):
    raw = copy.deepcopy(samples_by_engine["aws-textract"]["raw_output"])
    line = next(b for b in raw["Blocks"] if b["BlockType"] == "LINE")
    line.pop("Page")
    line["Id"] = "orphan-line"
    with pytest.raises(UnsupportedFormatError, match="not a CHILD"):
        ENGINES["aws-textract"].normalise(raw)
