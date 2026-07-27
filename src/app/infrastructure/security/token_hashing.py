"""Hashing for refresh tokens at rest.

A refresh token is stored only as a digest, so a database leak yields no usable
credential — the same principle as ``users.password_hash``.

**SHA-256, deliberately not Argon2.** Argon2's cost exists to defeat brute-force
guessing of low-entropy, human-chosen secrets. A refresh token is a signed,
high-entropy value that no attacker can enumerate, so a slow KDF would add
~50-100 ms to every refresh while adding no meaningful resistance. A plain
one-way digest is the correct tool here, and it is fast enough to leave the
event loop untouched.

The digest is *unsalted* on purpose: salting exists to stop precomputation
across a corpus of guessable inputs, which does not apply to random tokens, and
a deterministic digest is what allows a lookup by hash to work at all.
"""

from __future__ import annotations

import hashlib
import hmac

# A SHA-256 digest rendered as hexadecimal is always exactly 64 characters.
# `refresh_tokens.token_hash` is sized from this constant, so the column and the
# function that fills it cannot drift apart.
TOKEN_HASH_LENGTH = 64


def hash_token(token: str) -> str:
    """Return the hex digest to store for ``token``."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token_hash(token: str, token_hash: str) -> bool:
    """Return whether ``token`` matches a stored ``token_hash``.

    Compared with ``hmac.compare_digest`` rather than ``==``: a short-circuiting
    comparison leaks, through timing, how many leading characters of a guess
    were correct, which turns forging a hash into a character-by-character
    search instead of a search over the whole space.
    """

    return hmac.compare_digest(hash_token(token), token_hash)
