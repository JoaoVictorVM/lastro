from lastro.sources.chunking import chunk_document, chunking_fingerprint, spans_windows
from lastro.sources.embeddings import WordTokenizer
from lastro.sources.models import Extracted, Section


def _words(count: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


def test_800_token_chunks_and_100_overlap() -> None:
    text = _words(2000)
    document = Extracted(text, (Section("H", text, 0, line_start=1),))

    chunks = chunk_document(document, "a.md", WordTokenizer())

    assert [chunk.token_count for chunk in chunks] == [800, 800, 600]
    assert chunks[0].text.split()[-100:] == chunks[1].text.split()[:100]
    assert chunks[-1].text.endswith("w1999")
    assert all(chunk.heading == "H" for chunk in chunks)
    assert chunks[1].locator["char_start"] == chunks[1].start
    assert text[chunks[1].start : chunks[1].end] == chunks[1].text


def test_sections_and_pdf_pages_do_not_mix() -> None:
    first, second = "alfa beta", "gama delta"
    text = first + "\n\n" + second
    document = Extracted(
        text,
        (
            Section("Página 1", first, 0, page=1),
            Section("Página 2", second, len(first) + 2, page=2),
        ),
    )

    chunks = chunk_document(document, "m.pdf", WordTokenizer())

    assert [(chunk.text, chunk.locator["page_start"]) for chunk in chunks] == [
        ("alfa beta", 1),
        ("gama delta", 2),
    ]


def test_embedding_windows_cover_chunk_tail() -> None:
    text = _words(800)
    document = Extracted(text, (Section("H", text, 0),))

    (chunk,) = chunk_document(document, "a.md", WordTokenizer())

    assert chunk.windows == ((0, 480), (416, 800))
    assert chunk.window_texts[-1].endswith("w799")
    assert all(end - start <= 480 for start, end in chunk.windows)
    assert spans_windows(480, 480, 64) == [(0, 480)]
    assert spans_windows(0, 480, 64) == []


def test_line_locators_follow_the_section() -> None:
    body = "linha um\nlinha dois\nlinha três"
    document = Extracted(body, (Section("H", body, 0, line_start=5),))

    (chunk,) = chunk_document(document, "a.md", WordTokenizer())

    assert (chunk.locator["line_start"], chunk.locator["line_end"]) == (5, 7)


def test_tokenizer_fingerprint_changes_index() -> None:
    class Other(WordTokenizer):
        fingerprint = "b" * 64

    assert chunking_fingerprint(WordTokenizer()) != chunking_fingerprint(Other())
    assert len(chunking_fingerprint(WordTokenizer())) == 64
