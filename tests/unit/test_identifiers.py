"""Public identifier generation."""

from __future__ import annotations

from app.infrastructure.db.identifiers import PUBLIC_ID_LENGTH, new_public_id


def test_new_public_id_length() -> None:
    assert len(new_public_id()) == PUBLIC_ID_LENGTH == 26


def test_new_public_id_is_unique() -> None:
    ids = {new_public_id() for _ in range(1000)}
    assert len(ids) == 1000
