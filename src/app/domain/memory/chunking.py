"""Splitting a document into chunks.

**Deterministic, and that is the whole requirement.** Chunk identity is derived
from position (``<document>:<ordinal>``), so re-ingesting unchanged text must
produce byte-identical chunks in the same order — otherwise every re-ingest
would rewrite the index and the "unchanged content is a no-op" guarantee would
be a lie.

**Character windows, not tokens.** A token-based splitter would tie chunking to
one provider's tokenizer, so changing embedding model would silently re-chunk
every document in the corpus. Characters are provider-neutral and, for a POC,
close enough: the point of a chunk is to be a retrievable unit of meaning, not
to exactly fill a context window.

The one refinement over fixed windows is that a boundary is nudged back to
whitespace when one is close by, so chunks rarely split a word in half. It is
bounded (``_SNAP_WINDOW``) precisely so it cannot cascade: a text with no spaces
falls back to a hard cut rather than degenerating.

File formats — PDF, DOCX, HTML — are deliberately out of scope. Ingestion takes
text that something else has already extracted.
"""

from __future__ import annotations

from dataclasses import dataclass

CHUNK_SIZE = 1_000
"""Characters per chunk. Large enough to hold a paragraph or two of context,
small enough that a match points at something a person can read."""

CHUNK_OVERLAP = 200
"""Characters repeated from the end of the previous chunk.

Overlap exists because a sentence that answers a query may straddle a boundary,
and without it that sentence is retrievable from neither side. A fifth of the
chunk is the usual compromise: enough to carry a sentence across, small enough
that the corpus does not double."""

_SNAP_WINDOW = 100
"""How far back a boundary may move to land on whitespace. Bounded so a text
without spaces degrades to a hard cut instead of producing tiny chunks."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One chunk, and where it came from."""

    ordinal: int
    """Position in the document, from zero. Half of the chunk's identity."""

    text: str
    start: int
    """Character offset of this chunk in the source, so a match can be traced
    back to the original without storing the original twice."""

    @property
    def end(self) -> int:
        return self.start + len(self.text)


def chunk_text(text: str) -> list[Chunk]:
    """Split ``text`` into overlapping chunks, in order.

    Returns an empty list for text that is empty or only whitespace: a document
    with nothing in it has no chunks, and an "empty chunk" would be embedded,
    stored, and matched against every query for no reason.

    Operates on ``str`` throughout, so it is Unicode-safe by construction —
    slicing bytes would split multi-byte characters and produce chunks that are
    not valid text.
    """

    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped) <= CHUNK_SIZE:
        return [Chunk(ordinal=0, text=stripped, start=0)]

    chunks: list[Chunk] = []
    start = 0
    while start < len(stripped):
        end = _boundary(stripped, start)
        piece = stripped[start:end].strip()
        if piece:
            chunks.append(Chunk(ordinal=len(chunks), text=piece, start=start))
        if end >= len(stripped):
            break
        # Step forward by less than a full chunk, which is what creates the
        # overlap. Guarded against a non-advancing step so a pathological input
        # cannot loop forever.
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _boundary(text: str, start: int) -> int:
    """Where the chunk beginning at ``start`` should end.

    The hard limit, moved back to the nearest whitespace within ``_SNAP_WINDOW``
    so a chunk rarely ends mid-word.
    """

    hard = min(start + CHUNK_SIZE, len(text))
    if hard >= len(text):
        return hard

    window = text.rfind(" ", hard - _SNAP_WINDOW, hard)
    return window if window > start else hard
