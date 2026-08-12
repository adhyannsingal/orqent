"""The dependency rule, enforced mechanically (Phase 5, M5).

`architecture.md` §5 states the rule and `project_status.md` §12.3 records that
it has only ever been "enforced by convention and review". Convention holds
until someone adds an import in a hurry, and review catches it only if the
reviewer happens to look at the import block. These tests make the rule fail
loudly instead.

**Imports are read from the source, not by importing the module.** A test that
imported each package would measure what a *chain* of imports pulls in, so a
module would look impure because something it legitimately depends on is not —
and worse, it could pass simply because another test imported the offender
first. Parsing the AST asks the only question that matters: what does this file
itself declare?

Scope is deliberately the whole `src/app` tree rather than the workflow API
alone. The rule is not a Phase 5 rule; M5 is only where it stopped being
honour-based.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

import app

SRC = Path(app.__file__).parent

# Packages the domain must never reach for. `app.domain` may import itself and
# the standard library; anything else here would invert the dependency rule.
_OUTWARD = ("app.infrastructure", "app.services", "app.api", "app.container")

# Third-party machinery the domain must stay ignorant of. The domain is the part
# that has to survive replacing any of them (ADR-014).
_FRAMEWORKS = ("sqlalchemy", "fastapi", "starlette", "asyncmy", "alembic")


def _modules(package: str) -> Iterator[Path]:
    """Every Python file under a package, excluding caches."""

    subpath = package.removeprefix("app").lstrip(".").replace(".", "/")
    root = SRC / subpath if subpath else SRC
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def _imported_names(path: Path) -> set[str]:
    """Every module name this file imports, from the source alone.

    Both statement forms count. A relative import resolves to nothing here
    because the codebase uses absolute imports throughout, and a relative one
    would be a style break worth catching separately.
    """

    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _violations(path: Path, forbidden: tuple[str, ...]) -> set[str]:
    """Imports in this file that begin with any forbidden prefix."""

    return {
        name
        for name in _imported_names(path)
        if any(name == f or name.startswith(f"{f}.") for f in forbidden)
    }


def _relative(path: Path) -> str:
    return str(path.relative_to(SRC.parent))


# --- The domain depends on nothing outward -----------------------------------


@pytest.mark.parametrize("path", list(_modules("app.domain")), ids=_relative)
def test_the_domain_imports_no_outer_layer(path: Path) -> None:
    """`app.domain` is the centre; dependencies point inward (architecture.md §5)."""

    assert not _violations(path, _OUTWARD), f"{_relative(path)} reaches outward"


@pytest.mark.parametrize("path", list(_modules("app.domain")), ids=_relative)
def test_the_domain_imports_no_framework_or_driver(path: Path) -> None:
    """Pure Python, so the engine and the graph survive replacing any of them.

    Pydantic is the one permitted exception (ADR-031) and is deliberately not in
    the forbidden list: node contracts need it for config models and JSON Schema.
    """

    assert not _violations(path, _FRAMEWORKS), f"{_relative(path)} imports a framework"


# --- Transport schemas carry no machinery ------------------------------------


# `app.schemas` describes the JSON on the wire and nothing else. Both
# `schemas/auth.py` and `schemas/workflows.py` say so in their docstrings, and
# `routes/node_types.py` explains why the descriptor-to-response mapper lives in
# the route: so `app.schemas` "stays free of ORM and domain imports". Until now
# nothing enforced any of it — a schema could have imported a repository and the
# whole suite would still have passed.
_SCHEMA_FORBIDDEN = (*_OUTWARD, *_FRAMEWORKS, "app.domain")


@pytest.mark.parametrize("path", list(_modules("app.schemas")), ids=_relative)
def test_no_schema_imports_an_app_layer_or_framework(path: Path) -> None:
    """Transport models depend on Pydantic and the standard library, nothing else.

    ``app.domain`` is forbidden alongside the outer layers, which is stricter
    than the rule for routes and deliberately so: a schema importing a domain
    type would put the domain in the wire contract, and changing an enum member
    would silently become an API change. Routes map between the two precisely so
    that cannot happen.
    """

    assert not _violations(path, _SCHEMA_FORBIDDEN), f"{_relative(path)} is not transport-only"


# --- Routes are thin ----------------------------------------------------------


@pytest.mark.parametrize("path", list(_modules("app.api")), ids=_relative)
def test_no_route_module_imports_a_repository(path: Path) -> None:
    """Routes reach services, never persistence.

    A route that queried a repository would bypass the transaction boundary and
    the tenant scoping that live in the service — the exact shortcut M2 was
    written to avoid.
    """

    assert not _violations(path, ("app.infrastructure.repositories",)), _relative(path)


# Persistence *machinery* — the things that would let a route open a transaction
# or issue a query behind the service's back. Deliberately not the ORM model
# classes: ADR-008 makes those the data model rather than a persistence detail,
# and M10 chose `WorkflowNode` rows as `replace_graph`'s input, so the workflow
# routes construct them on the way in. That is the documented boundary, not a
# leak; what would be a leak is a session, an engine, or a repository.
_PERSISTENCE = (
    "sqlalchemy",
    "app.infrastructure.db.session",
    "app.infrastructure.db.engine",
    "app.infrastructure.db.unit_of_work",
    "app.infrastructure.repositories",
)


def test_the_workflow_routes_touch_no_persistence_machinery() -> None:
    """The workflow API surface specifically: no session, engine, or repository.

    `health.py` is exempt and stays so: its readiness probe runs `SELECT 1`
    deliberately, because opening a session proves less than a round trip does.
    That is a documented Phase 3B decision, not a leak — which is why this test
    names one module rather than sweeping the package.
    """

    workflows = SRC / "api" / "v1" / "routes" / "workflows.py"

    assert not _violations(workflows, _PERSISTENCE)


def test_the_workflow_routes_import_orm_models_only_for_the_service_boundary() -> None:
    """What the routes *do* take from infrastructure, pinned so it cannot grow.

    Two model classes, both required by the service signatures M10 settled. If a
    third appears, or anything that is not a model, this fails and the boundary
    gets re-argued rather than drifting.
    """

    workflows = SRC / "api" / "v1" / "routes" / "workflows.py"

    infrastructure = {
        name for name in _imported_names(workflows) if name.startswith("app.infrastructure")
    }

    assert infrastructure == {
        "app.infrastructure.db.models.workflow_node",
        "app.infrastructure.db.models.workflow_version",
    }


# --- Services hold no HTTP ------------------------------------------------------


@pytest.mark.parametrize("path", list(_modules("app.services")), ids=_relative)
def test_no_service_imports_fastapi(path: Path) -> None:
    """Services raise domain errors; the API layer alone maps them to status codes.

    A service importing FastAPI would be one `HTTPException` away from deciding
    transport concerns, and the error envelope would stop being the single thing
    `app.api.errors` renders.
    """

    assert not _violations(path, ("fastapi", "starlette")), _relative(path)


# --- Vendor containment ---------------------------------------------------------


def test_argon2_and_jwt_appear_only_in_the_security_adapters() -> None:
    """ADR-010's containment: one import site each, both behind ports.

    Asserted across the tree rather than by inspecting the two adapters, because
    the property is about everywhere *else*.
    """

    permitted = SRC / "infrastructure" / "security"
    offenders = [
        _relative(path)
        for path in _modules("app")
        if permitted not in path.parents and _violations(path, ("argon2", "jwt"))
    ]

    assert not offenders, f"argon2/jwt imported outside infrastructure/security: {offenders}"


def test_langchain_is_absent_from_the_entire_source_tree() -> None:
    """ADR-013 confines it to one future adapter; today it appears nowhere.

    Phase 5 has no execution, so this is not merely containment — it is proof
    that nothing has quietly pulled the runtime forward.
    """

    offenders = [_relative(path) for path in _modules("app") if _violations(path, ("langchain",))]

    assert not offenders, f"langchain imported: {offenders}"


# --- The guard has to be able to fail --------------------------------------------


def test_the_import_reader_sees_both_statement_forms(tmp_path: Path) -> None:
    """A guard that never fires is worse than none, so prove it fires.

    Without this, every assertion above would still pass if `_imported_names`
    silently returned nothing.
    """

    sample = tmp_path / "sample.py"
    sample.write_text(
        "import sqlalchemy\n"
        "from app.infrastructure.repositories import x\n"
        "from fastapi import APIRouter\n"
    )

    assert _imported_names(sample) == {
        "sqlalchemy",
        "app.infrastructure.repositories",
        "fastapi",
    }
    assert _violations(sample, _FRAMEWORKS) == {"sqlalchemy", "fastapi"}
    assert _violations(sample, _OUTWARD) == {"app.infrastructure.repositories"}


def test_every_layer_actually_has_modules_to_check() -> None:
    """Guards against a path typo quietly reducing the sweep to nothing."""

    for package, minimum in (
        ("app.domain", 15),
        ("app.api", 5),
        ("app.services", 2),
        ("app.schemas", 5),
    ):
        assert len(list(_modules(package))) >= minimum, package
