"""Refresh-token hashing (pure functions, no database)."""

from __future__ import annotations

import hashlib

import pytest

from app.infrastructure.security.token_hashing import (
    TOKEN_HASH_LENGTH,
    hash_token,
    verify_token_hash,
)

TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload.signature"


def test_hash_does_not_contain_the_token() -> None:
    digest = hash_token(TOKEN)

    assert TOKEN not in digest
    assert digest != TOKEN


def test_hash_is_hex_of_the_declared_length() -> None:
    # The column width is derived from this constant, so a change to one that
    # is not mirrored in the other truncates every stored hash.
    digest = hash_token(TOKEN)

    assert len(digest) == TOKEN_HASH_LENGTH
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_is_deterministic() -> None:
    # Unlike a password hash, this must be stable: a token is looked up and
    # compared by its digest, which a random salt would make impossible.
    assert hash_token(TOKEN) == hash_token(TOKEN)


def test_different_tokens_hash_differently() -> None:
    assert hash_token(TOKEN) != hash_token(TOKEN + "x")


def test_hash_matches_plain_sha256() -> None:
    # Pins the algorithm: silently switching it would invalidate every stored
    # hash and log every user out.
    assert hash_token(TOKEN) == hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()


def test_verify_accepts_a_matching_token() -> None:
    assert verify_token_hash(TOKEN, hash_token(TOKEN)) is True


def test_verify_rejects_a_different_token() -> None:
    assert verify_token_hash(TOKEN + "x", hash_token(TOKEN)) is False


@pytest.mark.parametrize("stored", ["", "not-a-hash", "0" * TOKEN_HASH_LENGTH])
def test_verify_rejects_a_malformed_stored_hash(stored: str) -> None:
    # A corrupt stored value must answer False, not raise: the caller's contract
    # is a boolean, and a database problem should not become a 500 at login.
    assert verify_token_hash(TOKEN, stored) is False


def test_verify_is_case_sensitive() -> None:
    # hexdigest() is lower-case; an upper-case stored value is not the same
    # string, and compare_digest does not fold case.
    assert verify_token_hash(TOKEN, hash_token(TOKEN).upper()) is False


def test_unicode_tokens_are_handled() -> None:
    # Explicit UTF-8 encoding, so a non-ASCII token cannot raise.
    assert len(hash_token("tökén-ünicode")) == TOKEN_HASH_LENGTH
