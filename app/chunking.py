"""Line-based chunking (docs/DESIGN.md §7). Pure functions, no DB access.

Per page, walk lines in reading order and accumulate whole lines until adding the
next one would push the chunk text (lines joined with "\\n") past MAX_CHARS; then
emit the chunk and start the next one with the previous chunk's last line
(overlap keeps a label together with a value on the following line). Chunks never
cross pages. A single line longer than MAX_CHARS becomes its own chunk.
"""

from collections.abc import Hashable, Sequence
from dataclasses import dataclass

CHUNKING_VERSION = "1.0"
MAX_CHARS = 400

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class ChunkLine:
    id: Hashable
    text: str
    bbox: BBox


@dataclass(frozen=True)
class ChunkDraft:
    text: str
    source_line_ids: list
    bbox: BBox
    char_count: int


def _joined_length(lines: Sequence[ChunkLine]) -> int:
    return sum(len(line.text) for line in lines) + max(len(lines) - 1, 0)


def _draft(lines: Sequence[ChunkLine]) -> ChunkDraft:
    text = "\n".join(line.text for line in lines)
    return ChunkDraft(
        text=text,
        source_line_ids=[line.id for line in lines],
        bbox=(
            min(line.bbox[0] for line in lines),
            min(line.bbox[1] for line in lines),
            max(line.bbox[2] for line in lines),
            max(line.bbox[3] for line in lines),
        ),
        char_count=len(text),
    )


def chunk_page(lines: Sequence[ChunkLine], max_chars: int = MAX_CHARS) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []
    current: list[ChunkLine] = []
    for line in lines:
        if current and _joined_length([*current, line]) > max_chars:
            chunks.append(_draft(current))
            overlap = current[-1]
            # Carry the overlap line only if it fits with the next line; otherwise the
            # next chunk would be the overlap line again on its own.
            current = [overlap] if _joined_length([overlap, line]) <= max_chars else []
        current.append(line)
    if current:
        chunks.append(_draft(current))
    return chunks


def chunk_pages(pages: Sequence[Sequence[ChunkLine]], max_chars: int = MAX_CHARS) -> list[list[ChunkDraft]]:
    """One list of chunks per page, in page order; chunk_index is assigned by the
    caller across the document."""
    return [chunk_page(lines, max_chars) for lines in pages]
