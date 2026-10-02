"""Azure Document Intelligence (analyzeResult, e.g. prebuilt-read) → normalised pages."""

from app.ocr import UnsupportedFormatError, as_number, raises_unsupported_format
from app.ocr.schema import NormalisedLine, NormalisedPage

ENGINE = "azure-document-intelligence"
# Azure reports `inch` for PDFs and `pixel` for images.
UNIT_MAP = {"inch": "inch", "pixel": "px"}


def check_shape(raw_output: dict) -> None:
    result = raw_output.get("analyzeResult") if isinstance(raw_output, dict) else None
    if not isinstance(result, dict):
        raise UnsupportedFormatError("azure raw_output.analyzeResult must be an object")
    if not isinstance(result.get("pages"), list):
        raise UnsupportedFormatError("azure analyzeResult.pages must be a list")
    if not isinstance(result.get("content"), str):
        raise UnsupportedFormatError("azure analyzeResult.content must be a string")


def _line_confidence(line: dict, words: list[dict]) -> float | None:
    """Mean confidence of the words whose span.offset falls in the line's spans[0]."""
    span = line["spans"][0]
    start = as_number(span["offset"], "line span offset")
    end = start + as_number(span["length"], "line span length")
    confs = [
        as_number(w["confidence"], "word confidence")
        for w in words
        if start <= as_number(w["span"]["offset"], "word span offset") < end
    ]
    return sum(confs) / len(confs) if confs else None


def _line(index: int, line: dict, words: list[dict], page_w: float, page_h: float) -> NormalisedLine:
    polygon = [as_number(v, "polygon value") for v in line["polygon"]]
    if len(polygon) < 8 or len(polygon) % 2:
        raise UnsupportedFormatError(f"azure line polygon must hold >= 4 x,y pairs, got {len(polygon)} numbers")
    xs, ys = polygon[0::2], polygon[1::2]
    return NormalisedLine(
        line_index=index,
        text=line["content"],
        bbox=(min(xs) / page_w, min(ys) / page_h, max(xs) / page_w, max(ys) / page_h),
        confidence=_line_confidence(line, words),
        confidence_source="mean_of_words",
    )


def _page(page: dict) -> NormalisedPage:
    unit = UNIT_MAP.get(page["unit"])
    if unit is None:
        raise UnsupportedFormatError(f"azure page unit {page['unit']!r} is not one of {list(UNIT_MAP)}")
    width = as_number(page["width"], "page width")
    height = as_number(page["height"], "page height")
    words = page.get("words", [])
    return NormalisedPage(
        page_number=page["pageNumber"],
        width=width,
        height=height,
        size_unit=unit,
        size_reason=None,
        lines=[_line(i, line, words, width, height) for i, line in enumerate(page.get("lines", []))],
    )


@raises_unsupported_format(ENGINE)
def normalise(raw_output: dict) -> list[NormalisedPage]:
    check_shape(raw_output)
    pages = [_page(p) for p in raw_output["analyzeResult"]["pages"]]
    return sorted(pages, key=lambda p: p.page_number)
