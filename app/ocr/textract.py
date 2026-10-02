"""AWS Textract (DetectDocumentText / AnalyzeDocument) → normalised pages."""

from collections import defaultdict

from app.ocr import UnsupportedFormatError, as_number, raises_unsupported_format
from app.ocr.schema import NormalisedLine, NormalisedPage

ENGINE = "aws-textract"
SIZE_REASON = "aws-textract reports only ratio coordinates, no absolute page size"


def check_shape(raw_output: dict) -> None:
    blocks = raw_output.get("Blocks") if isinstance(raw_output, dict) else None
    if not isinstance(blocks, list):
        raise UnsupportedFormatError("aws-textract raw_output.Blocks must be a list")
    if not any(isinstance(b, dict) and b.get("BlockType") == "PAGE" for b in blocks):
        raise UnsupportedFormatError("aws-textract raw_output.Blocks has no PAGE block")


def _page_of(block: dict) -> int:
    page = block["Page"]
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise UnsupportedFormatError(f"aws-textract block Page must be an int >= 1, got {page!r}")
    return page


def _line(index: int, block: dict) -> NormalisedLine:
    box = block["Geometry"]["BoundingBox"]
    left = as_number(box["Left"], "BoundingBox.Left")
    top = as_number(box["Top"], "BoundingBox.Top")
    width = as_number(box["Width"], "BoundingBox.Width")
    height = as_number(box["Height"], "BoundingBox.Height")
    return NormalisedLine(
        line_index=index,
        text=block["Text"],
        bbox=(left, top, left + width, top + height),
        confidence=as_number(block["Confidence"], "Confidence") / 100,
        confidence_source="engine_line",
    )


@raises_unsupported_format(ENGINE)
def normalise(raw_output: dict) -> list[NormalisedPage]:
    check_shape(raw_output)
    blocks = [b for b in raw_output["Blocks"] if isinstance(b, dict)]

    page_numbers = {_page_of(b) for b in blocks if b.get("BlockType") == "PAGE"}
    lines_by_page: dict[int, list[dict]] = defaultdict(list)
    for block in blocks:
        if block.get("BlockType") == "LINE":
            lines_by_page[_page_of(block)].append(block)
    page_numbers |= lines_by_page.keys()

    return [
        NormalisedPage(
            page_number=number,
            width=None,
            height=None,
            size_unit=None,
            size_reason=SIZE_REASON,
            lines=[_line(i, b) for i, b in enumerate(lines_by_page[number])],
        )
        for number in sorted(page_numbers)
    ]
