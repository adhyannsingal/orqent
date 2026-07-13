"""Centralized exception handling.

Registers three handlers that convert exceptions into the single
:class:`ErrorResponse` envelope:

* :class:`AppError` (and subclasses) → their declared ``http_status``/``code``.
* FastAPI ``RequestValidationError`` → 422 with per-field details.
* Any unhandled ``Exception`` → 500, logged with a stack trace, generic body.

Registering the base ``AppError`` handler is enough to cover every subclass
because Starlette walks the exception's MRO when selecting a handler.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status

from app.core.correlation import get_correlation_id
from app.domain.errors import AppError
from app.schemas.common import ErrorBody, ErrorDetail, ErrorResponse

log = structlog.get_logger(__name__)


def _render(http_status: int, code: str, message: str, details: list[ErrorDetail]) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            correlation_id=get_correlation_id(),
            details=details,
        )
    )
    return JSONResponse(status_code=http_status, content=body.model_dump())


async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    log.warning("app_error", code=exc.code, message=exc.message)
    details = [ErrorDetail.model_validate(d) for d in exc.details]
    return _render(exc.http_status, exc.code, exc.message, details)


async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        ErrorDetail(
            code="invalid",
            message=err.get("msg", "Invalid value"),
            field=".".join(str(p) for p in err.get("loc", ()) if p != "body"),
        )
        for err in exc.errors()
    ]
    return _render(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "validation_error",
        "Validation failed.",
        details,
    )


async def _handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_exception", error=str(exc))
    return _render(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "internal_error",
        "An unexpected error occurred.",
        [],
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all exception handlers to the application."""

    app.add_exception_handler(AppError, _handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected_error)
