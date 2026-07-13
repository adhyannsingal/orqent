# syntax=docker/dockerfile:1

# Build stage: asyncmy publishes no linux/arm64 wheels for Python 3.12, so it
# must compile from source; keep the toolchain out of the runtime image.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip wheel --wheel-dir /wheels .

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels multi-agent-platform \
    && rm -rf /wheels

# Drop privileges.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# One worker here; scale with a process manager / replicas in production.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
