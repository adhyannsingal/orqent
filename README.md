# Multi-Agent Orchestration Platform — Backend

FastAPI backend for a multi-agent orchestration platform. LangChain is confined
to the execution layer (later phase); the rest of the codebase is independent of
it. See `docs/` / the architecture document for the full design.

## Requirements

- Python 3.12+
- Docker + Docker Compose (optional, for the local infra stack)

## Quick start (local)

```bash
# 1. Create a virtualenv and install (editable + dev tooling)
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env

# 3. Run
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive API.

## Quality gates

```bash
ruff check .          # lint
ruff format .         # format
mypy src              # type-check
pytest                # tests
pre-commit install    # enable hooks (run automatically on commit)
```

## Docker

```bash
docker compose up --build     # api + MySQL + ChromaDB
```

## Health

- `GET /health/live`  — liveness (process up)
- `GET /health/ready` — readiness (dependency probes; expands in Phase 2+)

## Project layout

```
src/app/
  main.py            application factory
  container.py       DI composition root
  core/              config, logging, correlation, constants
  api/               routers, middleware, exception handlers, deps
  schemas/           Pydantic request/response models
  domain/            entities, value objects, ports, engine (pure)
  services/          use-case orchestration (Phase 4+)
  infrastructure/    repositories, llm/agent-runner, vector, queue, worker, security
```
