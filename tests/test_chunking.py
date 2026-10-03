from app.chunking import CHUNKING_VERSION, MAX_CHARS, ChunkLine, chunk_page, chunk_pages


def lines(*texts, page: int = 0):
    """Line i sits at y = i/100, x spans 0.1..0.1+len/1000."""
    return [
        ChunkLine(f"p{page}l{i}", t, (0.1, i / 100, 0.1 + len(t) / 1000, i / 100 + 0.01))
        for i, t in enumerate(texts)
    ]


def test_version():
    assert CHUNKING_VERSION == "1.0" and MAX_CHARS == 400


def test_short_page_is_one_chunk():
    page = lines("MEDICAL CERTIFICATE", "Patient Name:", "TAN WEI MING")
    [chunk] = chunk_page(page)
    assert chunk.text == "MEDICAL CERTIFICATE\nPatient Name:\nTAN WEI MING"
    assert chunk.source_line_ids == ["p0l0", "p0l1", "p0l2"]
    assert chunk.char_count == len(chunk.text)
    assert chunk.bbox == (0.1, 0.0, 0.1 + 19 / 1000, 0.03)  # union of the three boxes


def test_long_page_splits_with_one_line_overlap():
    page = lines(*[f"line {i:02d} " + "x" * 90 for i in range(12)])  # 98 chars per line
    chunks = chunk_page(page)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.char_count <= MAX_CHARS
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.source_line_ids[0] == prev.source_line_ids[-1]
    # Every line is covered, in order, and nothing beyond the overlap repeats.
    covered = [chunks[0].source_line_ids[0]] + [i for c in chunks for i in c.source_line_ids[1:]]
    assert covered == [line.id for line in page]


def test_chunk_never_exceeds_limit_at_the_boundary():
    # 4 lines of 99 chars joined by "\n" = 399 chars: fits exactly; a 5th must split.
    page = lines(*["y" * 99] * 5)
    first, second = chunk_page(page)
    assert first.char_count == 399 and len(first.source_line_ids) == 4
    assert second.source_line_ids == ["p0l3", "p0l4"]


def test_oversized_line_is_its_own_chunk_and_not_carried():
    page = lines("short", "z" * 450, "after")
    chunks = chunk_page(page)
    assert [c.source_line_ids for c in chunks] == [["p0l0"], ["p0l1"], ["p0l2"]]


def test_chunks_never_cross_pages():
    pages = [lines("page one a", "page one b", page=1), lines("page two", page=2)]
    per_page = chunk_pages(pages)
    assert [len(p) for p in per_page] == [1, 1]
    assert all(i.startswith("p1") for i in per_page[0][0].source_line_ids)
    assert per_page[1][0].source_line_ids == ["p2l0"]


def test_empty_page_has_no_chunks():
    assert chunk_page([]) == []
