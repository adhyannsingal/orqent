# Orqent — Multi-Agent Orchestration Platform (Backend)

Orqent is a backend platform for building and running **multi-agent AI
workflows**. A user defines agents (an LLM configuration + prompt), composes
them into a workflow, runs the workflow asynchronously, and gets a durable,
inspectable execution history.

**Guiding principle:** the workflow runtime is the product; the web framework
and the LLM library are replaceable details. Orqent owns orchestration,
persistence, and history — FastAPI is a thin HTTP edge, and LangChain (a later
phase) is confined to a single adapter.

> **Project status:** early, built phase by phase. Foundation, database
> infrastructure, and the initial migration are complete; authentication is
> next. See [`docs/project_status.md`](docs/project_status.md) for the live
> status and [`docs/roadmap.md`](docs/roadmap.md) for the phase plan.

## Architecture at a glance

Layered architecture with a hexagonal (ports & adapters) core for the parts
that must never couple to a vendor: the execution engine and the LLM
integration.

```
API (app.api)  →  Services (app.services)  →  Domain (app.domain, pure)
                                                  ↑ implements ports
                              Infrastructure (app.infrastructure, adapters)
Cross-cutting: app.core        Composition root: app.container
```

- **Dependency rule:** dependencies point inward. The domain imports no
  FastAPI/SQLAlchemy/LangChain/driver; only infrastructure imports vendors;
  only the container wires concretions.
- **Data:** MySQL 8 is the system of record; ChromaDB (later) is a derived,
  rebuildable vector index.
- Full design: [`docs/architecture.md`](docs/architecture.md); decisions and
  rationale: [`docs/decisions.md`](docs/decisions.md).

## Requirements

- Python 3.12+
- Docker + Docker Compose (for the local MySQL / ChromaDB stack)

## Quick start

### 1. Clone

```bash
git clone <repository-url> orqent
cd orqent
```

### 2. Create a virtualenv and install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # editable install + dev tooling
```

### 3. Configure

```bash
cp .env.example .env         # secret-free local defaults; edit as needed
```

Every setting is environment-driven with the `APP_` prefix (see
[`.env.example`](.env.example) for the full, commented list).

### 4. Start infrastructure (Docker)

```bash
# Full stack (API + MySQL + ChromaDB):
docker compose up --build

# Or hybrid — infra in Docker, app on the host (nicer for iterating):
docker compose up -d mysql chroma
```

Services: API `:8000`, MySQL `:3306`, ChromaDB `:8001`.

### 5. Run database migrations

Alembic reads the database URL from your environment (`APP_DATABASE_URL`). With
the compose MySQL running:

```bash
export APP_DATABASE_URL="mysql+asyncmy://app:app@127.0.0.1:3306/app"

alembic upgrade head        # apply all migrations
alembic current             # show the current revision
alembic downgrade -1        # roll back one revision
```

### 6. Run the API (host)

```bash
uvicorn app.main:app --reload
```

- Interactive docs: <http://localhost:8000/docs>
- Liveness: `GET /health/live` · Readiness: `GET /health/ready`
- The versioned business API mounts at `/api/v1` (empty until feature phases).

## Quality gates

All four must pass before every commit (and at the end of every phase):

```bash
ruff format .        # format
ruff check --fix .   # lint + import order
mypy src             # strict type-check
pytest               # tests
```

Or wire them as git hooks:

```bash
pre-commit install
pre-commit run --all-files
```

## Project structure

```
orqent/
├── alembic.ini             # Alembic config (DB URL injected from Settings)
├── docker-compose.yml      # api + MySQL + ChromaDB
├── Dockerfile              # multi-stage build (asyncmy compiled in builder)
├── pyproject.toml          # packaging, deps, ruff/mypy/pytest config
├── docs/                   # architecture, decisions (ADRs), roadmap, status
├── migrations/             # Alembic env + versioned migration scripts
└── src/app/
    ├── main.py             # FastAPI application factory (create_app)
    ├── container.py        # DI composition root
    ├── core/               # config, logging, correlation, constants
    ├── api/                # routers, middleware, exception handlers, deps
    ├── schemas/            # Pydantic request/response models
    ├── domain/             # entities, value objects, ports, engine (pure)
    ├── services/           # use-case orchestration (later phase)
    └── infrastructure/     # adapters: db, repositories, llm, vector, queue, ...
```

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, the required gates, and the
project's phase-discipline / architecture rules.
