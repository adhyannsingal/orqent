"""Request/execution correlation identifier.

Stored in a :class:`~contextvars.ContextVar` so it is available anywhere in the
async call stack without being threaded through every function signature. The
logging pipeline reads it to stamp every log line, which lets a single user
request (or later, a single execution) be traced end to end.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current context, if any."""

    return _correlation_id.get()


def set_correlation_id(value: str | None = None) -> str:
    """Bind a correlation id to the current context and return it.

    If ``value`` is falsy a new hex id is generated.
    """

    correlation_id = value or uuid.uuid4().hex
    _correlation_id.set(correlation_id)
    return correlation_id
