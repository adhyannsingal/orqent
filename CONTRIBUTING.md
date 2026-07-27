# Contributing to Orqent

Thanks for working on Orqent. This guide covers local setup and the standards
every change must meet. The deeper "why" lives in [`docs/`](docs/) — start with
[`docs/project_status.md`](docs/project_status.md) and
[`docs/CLAUDE.md`](docs/CLAUDE.md).

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
docker compose up -d mysql chroma   # infrastructure
```

See [`README.md`](README.md) for the full run/migrate flow.

## Quality gates (must pass before every commit)

All four are required; a phase is never "done" until they are green:

```bash
ruff format .        # format
ruff check --fix .   # lint (+ import order)
mypy src             # strict type-check
pytest               # tests
```

Enable them automatically with pre-commit:

```bash
pre-commit install
pre-commit run --all-files
```

## Architecture & phase discipline

Orqent is built **phase by phase**; each phase leaves a working, tested backend.
Before writing code:

- **Implement only the current phase.** Do not scaffold future phases or
  generate migrations before the phase that introduces their tables.
- **Respect the dependency rule and layer boundaries** — the domain imports
  nothing outward; only infrastructure imports vendors/drivers; only the
  container wires concretions. (See [`docs/architecture.md`](docs/architecture.md).)
- **Do not change an ADR** in [`docs/decisions.md`](docs/decisions.md) without
  raising it first and recording a new decision.
- **New tables:** model + mixins → metadata tests → autogenerate migration →
  **manual review of the revision** → apply → verify schema.

## Commits & branches

- Branch off `main`; keep changes scoped to one concern (SRP applies to diffs
  too — every changed line should trace to the stated goal).
- Write clear, imperative commit messages describing *what* and *why*.
- Update [`docs/project_status.md`](docs/project_status.md) when a phase or any
  repository infrastructure changes.
