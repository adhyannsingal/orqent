# Orqent — Multi-Agent Orchestration Platform (Backend)

Orqent is a backend platform for building and running **multi-agent AI
workflows**. A user defines agents, composes them into a workflow, runs the workflow asynchronously, and gets a durable,
inspectable execution history.

**Guiding principle:** the workflow runtime is the product; the web framework
and the LLM library are replaceable details. Orqent owns orchestration,
persistence, and history — FastAPI is a thin HTTP edge, and LangChain (a later
phase) is confined to a single adapter.



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
