"""HTTP middleware.

``CorrelationIdMiddleware`` establishes a correlation id for every request
(honouring an inbound ``X-Correlation-ID`` header, or minting one), binds it to
the logging context so all logs emitted while handling the request are
correlated, and echoes it back on the response so clients can quote it in bug
reports.

It also decides **what may be said about a request's URL**, which matters more
than it sounds: the bound ``path`` is attached to every log line emitted while
the request is handled, so a path that contains a credential would leak it into
every one of them — including the ones the error handler writes when the request
fails. See :func:`_loggable_path`.
"""

from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.constants import CORRELATION_ID_HEADER
from app.core.correlation import set_correlation_id

# Path prefixes whose next segment is a secret rather than an identifier. Today
# there is one: `POST /hooks/{token}` authenticates with a bearer token carried
# in the URL, because that is the only thing a third-party webhook sender can be
# relied on to send (Phase 9, M4).
_CREDENTIAL_BEARING_PREFIXES = ("/hooks/",)


def _loggable_path(path: str) -> str:
    """The request path with any credential in it removed.

    Redacted *here* rather than at each log call, because the path is bound once
    and then rides on every line — application logs, warnings from the error
    handler, and anything a later phase adds. Leaving it to callers would mean
    every future log statement had to remember.

    This does not protect a reverse proxy's or an ASGI server's own access log:
    those see the raw URL and are outside the application. A webhook token in a
    URL is exposed to whatever terminates TLS, which is inherent to the
    ``/hooks/{token}`` design and worth knowing rather than papering over.
    """

    for prefix in _CREDENTIAL_BEARING_PREFIXES:
        if path.startswith(prefix):
            return f"{prefix}<redacted>"
    return path


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(CORRELATION_ID_HEADER)
        correlation_id = set_correlation_id(incoming)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            path=_loggable_path(request.url.path),
            method=request.method,
        )

        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
