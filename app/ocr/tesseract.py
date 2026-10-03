"""Tesseract `image_to_data(output_type=DICT)` → normalised pages."""

from app.ocr import UnsupportedFormatError, as_number, raises_unsupported_format
from app.ocr.schema import NormalisedLine, NormalisedPage

ENGINE = "tesseract"
SHAPE_KEYS = ("level", "text", "conf", "left", "top", "width", "height")
GROUP_KEYS = ("page_num", "block_num", "par_num", "line_num")
LEVEL_PAGE = 1
LEVEL_WORD = 5


def check_shape(raw_output: dict) -> None:
    if not isinstance(raw_output, dict):
        raise UnsupportedFormatError("tesseract raw_output must be an object")
    missing = [k for k in SHAPE_KEYS if k not in raw_output]
    if missing:
        raise UnsupportedFormatError(f"tesseract raw_output is missing keys {missing}")
    if not all(isinstance(raw_output[k], list) for k in SHAPE_KEYS):
        raise UnsupportedFormatError("tesseract raw_output arrays must be lists")
    if len({len(raw_output[k]) for k in SHAPE_KEYS}) != 1:
        raise UnsupportedFormatError("tesseract raw_output arrays differ in length")


def _rows(raw_output: dict) -> list[dict]:
    """Transpose the parallel arrays into row dicts."""
    missing = [k for k in GROUP_KEYS if k not in raw_output]
    if missing:
        raise UnsupportedFormatError(f"tesseract raw_output is missing keys {missing}")
    keys = SHAPE_KEYS + GROUP_KEYS
    n = len(raw_output["level"])
    if any(not isinstance(raw_output[k], list) or len(raw_output[k]) != n for k in GROUP_KEYS):
        raise UnsupportedFormatError("tesseract raw_output arrays differ in length")
    return [{k: raw_output[k][i] for k in keys} for i in range(n)]


def _conf(value) -> float | None:
    """Word confidence 0–100 → 0–1; tesseract uses -1 for 'no confidence'.
    Older pytesseract versions emit conf as strings, so numeric strings are accepted."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise UnsupportedFormatError("tesseract conf is not numeric") from None
    conf = as_number(value, "tesseract conf")
    return None if conf < 0 else conf / 100


def _line(index: int, words: list[dict], page_w: float, page_h: float) -> NormalisedLine:
    x0 = min(as_number(w["left"], "left") for w in words)
    y0 = min(as_number(w["top"], "top") for w in words)
    x1 = max(as_number(w["left"], "left") + as_number(w["width"], "width") for w in words)
    y1 = max(as_number(w["top"], "top") + as_number(w["height"], "height") for w in words)
    confs = [c for c in (_conf(w["conf"]) for w in words) if c is not None]
    return NormalisedLine(
        line_index=index,
        text=" ".join(w["text"].strip() for w in words),
        bbox=(x0 / page_w, y0 / page_h, x1 / page_w, y1 / page_h),
        confidence=sum(confs) / len(confs) if confs else None,
        confidence_source="mean_of_words",
    )


@raises_unsupported_format(ENGINE)
def normalise(raw_output: dict) -> list[NormalisedPage]:
    check_shape(raw_output)
    rows = _rows(raw_output)

    page_sizes: dict[int, tuple[float, float]] = {}
    for row in rows:
        if row["level"] == LEVEL_PAGE:
            page_sizes[row["page_num"]] = (
                as_number(row["width"], "page width"),
                as_number(row["height"], "page height"),
            )

    # Words grouped per line, keyed in first-seen (engine reading) order.
    lines_by_page: dict[int, dict[tuple, list[dict]]] = {p: {} for p in page_sizes}
    for row in rows:
        if row["level"] != LEVEL_WORD or not isinstance(row["text"], str) or not row["text"].strip():
            continue
        page = row["page_num"]
        if page not in page_sizes:
            raise UnsupportedFormatError(f"tesseract words on page {page!r} without a level-1 page row")
        key = tuple(row[k] for k in GROUP_KEYS)
        lines_by_page[page].setdefault(key, []).append(row)

    pages = []
    for page in sorted(page_sizes):
        width, height = page_sizes[page]
        pages.append(
            NormalisedPage(
                page_number=page,
                width=width,
                height=height,
                size_unit="px",
                size_reason=None,
                lines=[
                    _line(i, words, width, height)
                    for i, words in enumerate(lines_by_page[page].values())
                ],
            )
        )
    return pages
