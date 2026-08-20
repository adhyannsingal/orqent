"""Document chunking (Phase 10, M4).

Pure and offline by construction: chunking is a decision about meaning, not
about infrastructure, so none of this needs a provider, a vector store, or a
database.

The property everything else rests on is **determinism** — chunk identity is
``<document>:<ordinal>``, so re-ingesting unchanged text must produce the same
chunks in the same order or the "unchanged content is a no-op" guarantee is a
lie and every re-ingest silently rewrites the index.
"""

from __future__ import annotations

import itertools

import pytest

from app.domain.memory.chunking import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    chunk_text,
)


def _long(words: int = 600) -> str:
    return " ".join(f"word{index}" for index in range(words))


# --- Emptiness ----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t  \n"])
def test_text_with_nothing_in_it_has_no_chunks(text: str) -> None:
    """An "empty chunk" would be embedded, stored, and matched against every
    query for no reason."""

    assert chunk_text(text) == []


def test_no_chunk_is_ever_empty() -> None:
    for chunk in chunk_text(_long()):
        assert chunk.text.strip()


# --- Small documents ----------------------------------------------------------


def test_a_short_document_is_one_chunk() -> None:
    chunks = chunk_text("A short note.")

    assert len(chunks) == 1
    assert chunks[0].text == "A short note."
    assert chunks[0].ordinal == 0


def test_surrounding_whitespace_is_not_part_of_the_document() -> None:
    """Otherwise the same text pasted with a trailing newline would hash
    differently and count as a change."""

    assert chunk_text("  hello  ")[0].text == "hello"


def test_a_document_exactly_at_the_limit_is_one_chunk() -> None:
    chunks = chunk_text("x" * CHUNK_SIZE)

    assert len(chunks) == 1


# --- Splitting ----------------------------------------------------------------


def test_a_long_document_is_split() -> None:
    chunks = chunk_text(_long())

    assert len(chunks) > 1


def test_no_chunk_exceeds_the_limit() -> None:
    """Bounded, so one enormous paragraph cannot become one enormous embedding
    request."""

    for chunk in chunk_text(_long(2000)):
        assert len(chunk.text) <= CHUNK_SIZE


def test_ordinals_are_consecutive_from_zero() -> None:
    """They are half of every chunk's identity, so a gap would leave a hole in
    the index that nothing would ever fill."""

    chunks = chunk_text(_long())

    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_source_order_is_preserved() -> None:
    chunks = chunk_text(_long())

    assert [chunk.start for chunk in chunks] == sorted(chunk.start for chunk in chunks)


def test_consecutive_chunks_overlap() -> None:
    """A sentence that answers a query may straddle a boundary; without overlap
    it is retrievable from neither side."""

    chunks = chunk_text(_long())
    assert len(chunks) >= 2

    first, second = chunks[0], chunks[1]
    assert second.start < first.end, "the second chunk should begin before the first ends"
    assert first.end - second.start >= CHUNK_OVERLAP // 2


def test_the_whole_document_is_covered() -> None:
    """Nothing between two chunks may be dropped, or a fact would be
    unretrievable while appearing to have been ingested."""

    chunks = chunk_text(_long())

    for previous, following in itertools.pairwise(chunks):
        assert following.start <= previous.end


def test_a_boundary_prefers_whitespace() -> None:
    """So a chunk rarely ends mid-word, which matters because the embedding is of
    the chunk's text as written."""

    chunks = chunk_text(_long())

    assert not chunks[0].text.endswith("wor")


def test_text_without_whitespace_still_chunks() -> None:
    """The snap-back is bounded precisely so this degrades to a hard cut rather
    than producing tiny chunks or looping."""

    chunks = chunk_text("x" * (CHUNK_SIZE * 3))

    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.text


# --- Determinism --------------------------------------------------------------


def test_chunking_is_deterministic() -> None:
    """The property the whole re-ingestion policy rests on."""

    text = _long()

    first = [(c.ordinal, c.text, c.start) for c in chunk_text(text)]
    second = [(c.ordinal, c.text, c.start) for c in chunk_text(text)]

    assert first == second


def test_identical_content_chunks_identically_regardless_of_origin() -> None:
    """Two different documents containing the same text produce the same chunk
    *text* — they stay distinct because identity comes from the document, not
    from the content."""

    assert [c.text for c in chunk_text(_long())] == [c.text for c in chunk_text(_long())]


def test_changed_content_chunks_differently() -> None:
    assert [c.text for c in chunk_text(_long())] != [c.text for c in chunk_text(_long(601))]


# --- Unicode ------------------------------------------------------------------


def test_unicode_survives_chunking() -> None:
    """Operating on `str` rather than bytes is what makes this true by
    construction: slicing bytes would split multi-byte characters and produce
    text that is not valid."""

    text = "héllo wörld 😀 مرحبا 你好"

    assert chunk_text(text)[0].text == text


def test_long_unicode_text_chunks_without_corruption() -> None:
    text = "😀 你好 مرحبا " * 400

    chunks = chunk_text(text)

    assert len(chunks) > 1
    for chunk in chunks:
        # Would raise on a lone surrogate or a split code point.
        chunk.text.encode("utf-8")


def test_offsets_are_character_offsets_not_byte_offsets() -> None:
    text = "😀" * 10 + " tail"

    chunk = chunk_text(text)[0]

    assert chunk.start == 0
    assert chunk.end == len(chunk.text)
