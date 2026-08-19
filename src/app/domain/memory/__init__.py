"""Knowledge domain — how a document becomes retrievable units.

Pure: no database, no provider, no vector store. Splitting a document into
chunks is a decision about *meaning* rather than about infrastructure, and
keeping it here is what lets it be tested exhaustively with no network and no
MySQL (ADR-014).
"""
