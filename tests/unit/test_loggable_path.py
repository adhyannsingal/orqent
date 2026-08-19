"""Redacting credentials out of the logged request path (Phase 9, M4).

``CorrelationIdMiddleware`` binds the request path once and it then rides on
every log line the request produces. ``POST /hooks/{token}`` puts a bearer
credential in that path, so the binding has to be redacted or every line —
including the error handler's — is a credential leak.
"""

from __future__ import annotations

import pytest

from app.api.middleware import _loggable_path


@pytest.mark.parametrize(
    "path",
    [
        "/hooks/OupVxh2BSVrMsFCJ9AivLcvKjkQJnEeYUgSnflUY7Rk",
        "/hooks/short",
        "/hooks/with/extra/segments",
    ],
)
def test_a_hooks_path_keeps_nothing_after_the_prefix(path: str) -> None:
    redacted = _loggable_path(path)

    assert redacted == "/hooks/<redacted>"
    # The whole point: no fragment of the credential survives, so a log line
    # cannot be used to narrow a guess.
    assert path.removeprefix("/hooks/") not in redacted


def test_an_empty_token_is_still_redacted() -> None:
    """Not a real request — the route would not match — but the rule must not
    depend on the token being non-empty."""

    assert _loggable_path("/hooks/") == "/hooks/<redacted>"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/workflows/01WORKFLOW",
        "/api/v1/runs",
        "/health/ready",
        "/",
        # Not the hooks prefix: a path that merely mentions hooks is ordinary.
        "/api/v1/webhooks/settings",
        "/hooksomething",
    ],
)
def test_an_ordinary_path_is_logged_intact(path: str) -> None:
    """Redaction must not blind the logs generally — a path is how anyone finds
    the request they are looking for."""

    assert _loggable_path(path) == path
