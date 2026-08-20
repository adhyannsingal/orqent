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
import tokenize
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, SecretStr

import app
from app.core.config import Settings
from app.domain.engine.events import RunEventType
from app.domain.nodes.descriptor import SideEffect
from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.base import Base
from app.infrastructure.nodes import build_registry
from app.infrastructure.tools import CATALOGUE

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


# --- The engine knows no node type (Phase 6, M7) -----------------------------

# Every module that decides *what runs next* or *what a result means*. None of
# them may name a node type: the engine reacts to `Suspended`, never to
# `core.wait` (ADR-014, ADR-020). This is the mechanical form of the claim that
# adding a node type touches no engine code.
_ENGINE_MODULES = (
    "domain/engine/scheduler.py",
    "domain/engine/snapshot.py",
    "domain/engine/invocation.py",
    "domain/engine/state.py",
    "domain/engine/events.py",
    "services/run_service.py",
)

# The whole built-in catalogue, so a future node is covered without editing this.
_NODE_TYPES = tuple(descriptor.node_type for descriptor in build_registry().all())


def _code_only(path: Path) -> str:
    """The module's source with comments and string literals removed.

    Prose may name a node type — `scheduler.py`'s docstring cites
    ``core.constant`` as the example of a second zero-inbound node, and that
    explains the rule rather than breaking it. What must never appear is a node
    type the *code* depends on.
    """

    with tokenize.open(path) as handle:
        return " ".join(
            token.string
            for token in tokenize.generate_tokens(handle.readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )


@pytest.mark.parametrize("module", _ENGINE_MODULES)
def test_the_engine_names_no_node_type(module: str) -> None:
    """Suspension is the sharpest case: the engine must react to the *result*
    type a runner returned, not to which node returned it."""

    code = _code_only(SRC / module)
    named = [node_type for node_type in _NODE_TYPES if node_type in code]

    assert not named, f"{module} names node types: {named}"


@pytest.mark.parametrize("module", _ENGINE_MODULES)
def test_the_engine_imports_no_concrete_node(module: str) -> None:
    """Runners are resolved through the `NodeRegistry` port, never imported."""

    assert not _violations(SRC / module, ("app.infrastructure.nodes",))


def test_the_node_type_guard_actually_has_types_to_check() -> None:
    """A registry that returned nothing would make the guard above vacuous."""

    assert len(_NODE_TYPES) >= 5
    assert "core.wait" in _NODE_TYPES


# --- Phase 9: triggers must not smuggle mechanics into the graph -------------
#
# Phase 9 gave the platform two ways to start a run without a user — an HTTP
# request and a clock. Both are the kind of feature that leaks: the easy place to
# put "fire at 10:00" is the node that says "I fire at 10:00", and the easy way to
# start a run for nobody is to invent a nobody. These pin the boundaries that
# were argued for instead.

_TRIGGER_RUNNERS = (
    "infrastructure/nodes/builtin/trigger_manual.py",
    "infrastructure/nodes/builtin/trigger_webhook.py",
    "infrastructure/nodes/builtin/trigger_schedule.py",
)

# What a *runner* must not do. A node is handed everything it needs in its
# context; reaching past that makes it untestable, non-deterministic, or both —
# and at-least-once delivery (ADR-024) means a runner that reads a clock gives
# two different answers for one firing.
_RUNNER_FORBIDDEN = (
    "uow",
    "session",
    "queue",
    "repositor",
    "httpx",
    "requests",
    "Schedule",
    "TriggerRegistration",
)


def _runner_body(path: Path) -> str:
    """The source of every ``run`` method in a module, comments and docstrings
    stripped — so prose *about* clocks does not count as touching one."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    bodies: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "run":
            stripped = ast.parse(ast.unparse(node))
            for inner in ast.walk(stripped):
                if isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Constant):
                    inner.value.value = ""
            bodies.append(ast.unparse(stripped))
    return "\n".join(bodies)


@pytest.mark.parametrize("module", _TRIGGER_RUNNERS)
def test_a_trigger_runner_contains_no_dispatch_mechanics(module: str) -> None:
    """A trigger emits what it was given. It does not decide *that* it fires."""

    body = _runner_body(SRC / module)
    assert body, f"{module} has no run method to check"

    found = [word for word in _RUNNER_FORBIDDEN if word in body]
    assert not found, f"{module}'s runner reaches for {found}"


@pytest.mark.parametrize("module", _TRIGGER_RUNNERS)
def test_a_trigger_runner_does_not_read_the_clock(module: str) -> None:
    """The one that would look most reasonable in a schedule trigger.

    A run is invoked at least once and may be re-invoked after a crash
    (ADR-024). A runner that read the clock would report a different
    ``scheduled_for`` on the retry than the occurrence it was actually dispatched
    for — so the moment is decided once, by the dispatcher, and carried in.
    """

    body = _runner_body(SRC / module)

    for reading in ("now(", "utcnow", "time()", "monotonic"):
        assert reading not in body, f"{module}'s runner reads the clock via {reading}"


def _constructs(path: Path, name: str) -> bool:
    """Whether this module *calls* ``name``.

    Asked of the AST rather than the text, so an import or a type annotation does
    not count — only actually building one does.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(tree)
    )


def test_only_authentication_constructs_an_authenticated_user() -> None:
    """No synthetic users on the webhook, worker, or dispatcher paths.

    All three act for a tenant with no person behind them, and the tempting fix
    is to invent an ``AuthenticatedUser`` to satisfy a signature. It would fail
    anyway — the services look the account up — and inventing a *row* to make it
    succeed would put someone's name on a run they did not start, turning the
    audit trail into a fiction. The tenant comes from the registration, the
    claimed task, or the schedule instead.
    """

    constructing = [
        path.relative_to(SRC).as_posix()
        for path in _modules("app")
        if _constructs(path, "AuthenticatedUser")
    ]

    assert sorted(constructing) == [
        # Rebuilt from a verified token.
        "api/security.py",
        # Issued after a password was checked.
        "services/auth_service.py",
    ], constructing


def test_the_queue_and_worker_know_nothing_about_triggers() -> None:
    """Phase 8 stayed generic. A scheduled run and a webhook-started run are
    ordinary runs, so neither the queue nor the worker needed a word about
    either — which is what "no second execution path" means in practice."""

    for package in ("app.infrastructure.queue", "app.infrastructure.worker"):
        for path in _modules(package):
            code = _code_only(path)
            for word in ("webhook", "schedule", "Schedule", "cron", "trigger"):
                assert word not in code, f"{path.name} mentions {word}"


def test_the_cron_expression_has_one_home() -> None:
    """The schedule's definition lives in the published node's config, and the
    ``schedules`` table holds only runtime state. Two copies could disagree about
    when a workflow runs, and the row is the one that would silently win."""

    columns = set(Base.metadata.tables["schedules"].c.keys())

    assert "cron" not in columns
    assert "timezone" not in columns


# --- Phase 10: the AI layer must not leak (ADR-013) --------------------------
#
# The redesign's sharpest claim is that AI is a *supporting* subdomain: an agent
# step is dispatched, retried, and recorded by exactly the machinery that handles
# a no-op. That claim is only true while the provider vocabulary stays behind one
# port and one adapter package, and it is the kind of thing that erodes one
# convenient import at a time. These make the erosion fail loudly.

# LangChain and every vendor SDK. `chromadb` is here too: retrieval arrives in a
# later Phase 10 milestone and belongs to the same adapter package, not to a node
# or the engine (ADR-003, rescoped).
_AI_PACKAGES = (
    "langchain",
    "langchain_google_genai",
    "langchain_core",
    "google",
    "langgraph",
    "openai",
    "anthropic",
    "chromadb",
    "chroma",
    "tiktoken",
    "litellm",
    "transformers",
)

# The one package permitted to import them, when they arrive (M2 onward). Its own
# docstring has said so since Phase 1.
# **A set, not a single package, since M4.** The rule is unchanged — only an
# approved adapter may import a vendor SDK — but there are now two such adapters:
# LangChain/Gemini for generation and embedding, and ChromaDB for vectors
# (ADR-003). Widening the *list* is a visible, reviewable edit; widening the rule
# to "infrastructure may import anything" would not be.
_ADAPTER_PACKAGES = ("app/infrastructure/llm", "app/infrastructure/vector")


def _ai_imports(path: Path) -> list[str]:
    return sorted(name for name in _imported_names(path) if name.split(".")[0] in _AI_PACKAGES)


@pytest.mark.parametrize("path", list(_modules("app")), ids=lambda p: p.name)
def test_only_the_llm_adapter_may_import_a_provider(path: Path) -> None:
    """ADR-013, mechanically.

    Not "the engine does not import LangChain" but "**nothing** does, except one
    package". The weaker rule is the one that rots: the tempting import is never
    in the scheduler, it is in a node that needs "just a token count" or a
    service that wants "just an embedding".
    """

    leaked = _ai_imports(path)
    if leaked and any(package in path.as_posix() for package in _ADAPTER_PACKAGES):
        return
    assert not leaked, f"{path.relative_to(SRC)} imports {leaked}"


def test_the_domain_never_imports_a_provider() -> None:
    """Stated separately from the rule above because it is the one that matters
    most and must survive the adapter exception being edited."""

    for path in _modules("app.domain"):
        assert not _ai_imports(path), f"{path.relative_to(SRC)} reaches a provider"


@pytest.mark.parametrize("module", _ENGINE_MODULES)
def test_the_engine_does_not_know_the_agent_port_exists(module: str) -> None:
    """ADR-014's strengthening, and the reason `AgentRunner` is *not* an engine
    dependency: it is an implementation detail of one node's runner. The engine
    depends on `NodeRunner` and resolves runners through a registry it does not
    own."""

    assert not _violations(SRC / module, ("app.domain.ports.agent_runner",))


def test_the_agent_node_reaches_a_model_only_through_the_port() -> None:
    """The node is where a direct SDK import would be most convenient and most
    damaging — it would make provider choice a property of a *published version*
    rather than of the deployment."""

    node = SRC / "infrastructure/nodes/builtin/ai_agent.py"
    imported = _imported_names(node)

    assert not _ai_imports(node)
    assert "app.domain.ports.agent_runner" in imported


def test_no_node_configuration_can_carry_a_credential() -> None:
    """Node config is stored in `workflow_nodes.config`: plain JSON inside an
    immutable published version, readable by anyone who can read the workflow,
    copied into every republish, and impossible to rotate without republishing.

    Asked of the whole catalogue rather than of `ai.agent` alone, because the
    node most likely to want an API key next is the HTTP node, and this should
    already be failing when someone tries.
    """

    forbidden = ("api_key", "apikey", "secret", "token", "password", "credential")

    for descriptor in build_registry().all():
        for field in descriptor.config_model.model_fields:
            lowered = field.lower().replace("_", "")
            for word in forbidden:
                assert word.replace("_", "") not in lowered, (
                    f"{descriptor.qualified_name} config field {field!r} looks like a credential"
                )


def test_the_provider_detector_actually_detects(tmp_path: Path) -> None:
    """A self-check, because the guards above are otherwise unfalsifiable here.

    None of the forbidden packages is installed, so mutating a real module to
    import one fails at *collection* rather than at the assertion — which proves
    the package is absent, not that the rule works. Handing the detector a file
    that does import one closes that gap.
    """

    offender = tmp_path / "offender.py"
    offender.write_text(
        "import langchain\n"
        "from openai import OpenAI\n"
        "import chromadb.config\n"
        "from app.domain.nodes.runner import NodeRunner\n",
        encoding="utf-8",
    )

    assert _ai_imports(offender) == ["chromadb.config", "langchain", "openai"]

    innocent = tmp_path / "innocent.py"
    innocent.write_text("from app.domain.ports.agent_runner import AgentRunner\n", encoding="utf-8")

    assert _ai_imports(innocent) == []


# --- Phase 10 M2: the provider is installed now, so the guards must be real ---
#
# M1's guards were written while none of the forbidden packages existed in the
# environment. That is no longer true: `langchain-google-genai` and `google-genai`
# are installed, so an accidental import in the wrong layer would now *succeed*
# and the guards are doing load-bearing work rather than describing an intention.

_PROVIDER_FREE_PACKAGES = (
    "app.domain",
    "app.services",
    "app.api",
    "app.infrastructure.queue",
    "app.infrastructure.worker",
    "app.infrastructure.dispatcher",
    "app.infrastructure.db",
    "app.infrastructure.repositories",
    "app.infrastructure.nodes",
)


@pytest.mark.parametrize("package", _PROVIDER_FREE_PACKAGES)
def test_no_layer_but_the_adapter_imports_a_provider(package: str) -> None:
    """Every layer the AI must not reach, named individually.

    Listed rather than inferred so that a *new* package is not silently exempt:
    adding one to `src/app` and forgetting it here is a visible omission in this
    list, whereas a clever "everything except the adapter" rule would cover it
    automatically and hide the decision.

    `app.infrastructure.nodes` is in the list and matters most: `ai.agent@1`
    lives there, and it is the single most convenient place to reach for the SDK
    directly — which would make provider choice a property of a *published
    version* rather than of the deployment.
    """

    for path in _modules(package):
        assert not _ai_imports(path), f"{path.relative_to(SRC)} imports a provider"


def test_the_adapter_package_is_where_the_provider_lives() -> None:
    """The positive half. A guard that only forbids would still pass if the
    integration were deleted, so this asserts the import is where it should be."""

    adapter = SRC / "infrastructure/llm/gemini_agent_runner.py"
    imported = _imported_names(adapter)

    assert any(name.startswith("langchain_google_genai") for name in imported)
    assert "app.domain.ports.agent_runner" in imported


def test_the_settings_credential_is_a_secret_type() -> None:
    """`SecretStr` is what stops a settings dump, a traceback frame, or a logged
    model object from printing the key. A plain `str` would leak on any of
    them."""

    annotation = Settings.model_fields["gemini_api_key"].annotation

    assert SecretStr in getattr(annotation, "__args__", (annotation,))


def test_no_api_schema_exposes_the_credential() -> None:
    """Nothing the HTTP surface returns may carry it — the credential is
    server-side only and has no reason to appear in a response model."""

    for path in _modules("app.schemas"):
        code = _code_only(path).lower()
        for word in ("gemini", "api_key", "apikey"):
            assert word not in code, f"{path.name} mentions {word}"


def test_no_workflow_configuration_can_carry_a_provider_credential() -> None:
    """Restated for M2 in the terms that now matter: node config is JSON inside
    an immutable published version, so a Gemini key there would be readable by
    anyone who can read the workflow and unrotatable without republishing."""

    for descriptor in build_registry().all():
        schema = descriptor.config_model.model_json_schema()
        rendered = str(schema).lower()
        for word in ("gemini", "api_key", "apikey", "google_api_key"):
            assert word not in rendered, f"{descriptor.qualified_name} config mentions {word}"


def test_the_event_vocabulary_stays_free_of_provider_concepts() -> None:
    """An AI step is a *node* step, so the timeline says exactly what it says for
    a no-op (Phase 10, M3).

    ``AgentStarted``, ``LLMCalled``, ``TokenUsage``, ``PromptSent`` are all
    plausible and all wrong here: they would put provider vocabulary into the
    engine's basic language, which is the coupling ADR-013 exists to prevent, and
    every future provider would then argue about which of its concepts deserve an
    event. Observability of that kind is later work built *beside* the engine,
    not inside its event model.

    Pinned as an exact set rather than a denylist, so a new event type is a
    deliberate decision with a reason rather than something that slipped in.
    """

    assert {event.value for event in RunEventType} == {
        "RunStarted",
        "RunSuspended",
        "RunResumed",
        "RunCompleted",
        "RunFailed",
        "NodeStarted",
        "NodeSucceeded",
        "NodeFailed",
        "NodeSuspended",
        "NodeSkipped",
    }


# --- Phase 10 M4: retrieval stays behind its ports ---------------------------


def test_the_chroma_adapter_is_where_the_vector_store_lives() -> None:
    """The positive half. A rule that only forbids would still pass if the
    integration were deleted."""

    adapter = SRC / "infrastructure/vector/chroma_store.py"
    imported = _imported_names(adapter)

    assert any(name.startswith("chromadb") for name in imported)
    assert "app.domain.ports.vector_store" in imported


def test_the_memory_service_speaks_only_in_ports() -> None:
    """Ingestion and retrieval orchestrate; they must not know which vector
    database or which embedding provider is underneath (ADR-003)."""

    service = SRC / "services/memory_service.py"

    assert not _ai_imports(service)
    imported = _imported_names(service)
    assert "app.domain.ports.embedder" in imported
    assert "app.domain.ports.vector_store" in imported


def test_the_agent_node_still_knows_nothing_about_retrieval() -> None:
    """**The M5 boundary, enforced rather than promised.**

    Joining retrieval to generation is the next milestone, and the temptation is
    to do it here "while the code is open". Until M5 decides how, an agent must
    not import a retriever, a vector store, or the memory service.
    """

    node = SRC / "infrastructure/nodes/builtin/ai_agent.py"

    assert not _violations(
        node,
        (
            "app.domain.ports.vector_store",
            "app.domain.ports.embedder",
            "app.services.memory_service",
            "app.infrastructure.vector",
        ),
    )


def test_the_gemini_agent_runner_still_knows_nothing_about_chroma() -> None:
    """Same boundary, one layer down: generation and retrieval are two systems
    until M5 joins them."""

    runner = SRC / "infrastructure/llm/gemini_agent_runner.py"
    imported = _imported_names(runner)

    assert not any(name.startswith("chromadb") for name in imported)
    assert "app.domain.ports.vector_store" not in imported


def test_no_layer_outside_the_adapters_imports_chromadb() -> None:
    """Stated separately from the general provider rule because `chromadb` is now
    installed, so a misplaced import would resolve rather than fail loudly."""

    for package in _PROVIDER_FREE_PACKAGES:
        for path in _modules(package):
            imported = _imported_names(path)
            assert not any(name.startswith("chromadb") for name in imported), path.name


# =============================================================================
# Retrieval-augmented generation (Phase 10, M5)
# =============================================================================
#
# M5 joins retrieval to generation. The risk it introduces is not that RAG works
# badly — it is that RAG becomes *load-bearing*: that the engine learns to
# schedule around it, that the queue learns to carry it, or that the tenant it
# retrieves from becomes something a workflow can name. These guards say none of
# that happened.

_RAG_VOCABULARY = (
    "retriev",
    "knowledge",
    "chroma",
    "embed",
    "rag",
    "augment",
    "vector",
    "chunk",
)


@pytest.mark.parametrize("module", _ENGINE_MODULES)
def test_the_engine_has_no_vocabulary_for_retrieval(module: str) -> None:
    """The engine schedules nodes; it does not know some of them read documents.

    Checked against the *source text* rather than imports, because the way this
    boundary actually erodes is a special case — "if the node retrieves, give it
    longer" — not an import anyone would notice in review.
    """

    source = _code_only(SRC / module).lower()
    found = [word for word in _RAG_VOCABULARY if word in source]

    assert not found, f"{module} mentions {found}"


@pytest.mark.parametrize("module", _ENGINE_MODULES)
def test_the_engine_does_not_know_the_knowledge_port_exists(module: str) -> None:
    """The same statement as for ``AgentRunner``, for the same reason: retrieval
    is an implementation detail of one node's runner (ADR-014, ADR-020)."""

    assert not _violations(
        SRC / module,
        (
            "app.domain.ports.knowledge",
            "app.domain.ports.vector_store",
            "app.services.memory_service",
        ),
    )


@pytest.mark.parametrize("package", ["app.infrastructure.queue", "app.infrastructure.worker"])
def test_the_queue_and_worker_know_nothing_about_retrieval(package: str) -> None:
    """A queued AI node is a queued node. Nothing about the transport changes
    because the work it names happens to read documents first."""

    for path in _modules(package):
        source = _code_only(path).lower()
        found = [word for word in _RAG_VOCABULARY if word in source]
        assert not found, f"{_relative(path)} mentions {found}"


def test_the_dispatcher_knows_nothing_about_retrieval() -> None:
    for path in _modules("app.infrastructure.dispatcher"):
        source = _code_only(path).lower()
        assert not [word for word in _RAG_VOCABULARY if word in source], _relative(path)


def test_the_run_service_never_reaches_a_vector_store() -> None:
    """``RunService`` resolves the tenant and hands it to a node. That is the
    whole of its involvement in M5, and it must not grow: a run service that
    could retrieve would be a run service that could retrieve for the wrong
    tenant."""

    assert not _violations(
        SRC / "services/run_service.py",
        (
            "app.domain.ports.vector_store",
            "app.domain.ports.knowledge",
            "app.domain.ports.embedder",
            "app.services.memory_service",
            "app.infrastructure.vector",
        ),
    )


def test_the_agent_node_reaches_documents_only_through_the_port() -> None:
    """The node composes retrieval and generation, so it is the module where a
    shortcut to Chroma or to ``MemoryService`` would be most convenient."""

    node = SRC / "infrastructure/nodes/builtin/ai_agent.py"

    assert not _violations(
        node,
        (
            "app.services",
            "app.infrastructure.vector",
            "app.infrastructure.llm",
            "app.domain.ports.embedder",
            "app.domain.ports.vector_store",
        ),
    )


def test_no_node_configuration_can_name_a_tenant() -> None:
    """**The tenant is engine-supplied, and this is the mechanical statement of
    it.** A config field naming an organization would be a field an author could
    set, stored in a published version, and read at execution — which is exactly
    the cross-tenant path M5 exists to make impossible.

    Checked across the whole catalogue rather than ``ai.agent@1`` alone: the next
    tenant-scoped node type is the one that would get this wrong.
    """

    forbidden = {"organization", "organization_id", "organization_public_id", "tenant", "org_id"}

    for descriptor in build_registry().all():
        config_models = [descriptor.config_model]
        # Nested models are where it would actually appear — `retrieval.org_id`
        # rather than a top-level field somebody would notice.
        for field in descriptor.config_model.model_fields.values():
            for candidate in getattr(field.annotation, "__args__", ()) or (field.annotation,):
                if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                    config_models.append(candidate)

        for model in config_models:
            leaked = forbidden & set(model.model_fields)
            assert not leaked, f"{descriptor.qualified_name} config exposes {leaked}"


# =============================================================================
# Tools and tool calling (Phase 10, M6)
# =============================================================================
#
# M6 lets a model *act*. The risk is the same shape as M5's and worse in
# consequence: not that tools work badly, but that tool calling becomes
# something the engine schedules, the queue carries, or a provider adapter gets
# to decide the rules of. A whole tool conversation is meant to happen inside one
# ordinary node invocation, and these say it does.

_TOOL_VOCABULARY = ("tool", "function_call", "bind_tools")


@pytest.mark.parametrize("module", _ENGINE_MODULES)
def test_the_engine_has_no_vocabulary_for_tools(module: str) -> None:
    """A tool-using node execution is a node execution.

    Checked against source text rather than imports, because this boundary
    erodes through a special case — "give a tool-using agent a longer lease" —
    that no import would reveal.
    """

    source = _code_only(SRC / module).lower()
    found = [word for word in _TOOL_VOCABULARY if word in source]

    assert not found, f"{module} mentions {found}"


@pytest.mark.parametrize("module", _ENGINE_MODULES)
def test_the_engine_does_not_know_the_tool_contract_exists(module: str) -> None:
    assert not _violations(SRC / module, ("app.domain.tools", "app.infrastructure.tools"))


@pytest.mark.parametrize(
    "package",
    ["app.infrastructure.queue", "app.infrastructure.worker", "app.infrastructure.dispatcher"],
)
def test_the_transport_knows_nothing_about_tools(package: str) -> None:
    for path in _modules(package):
        source = _code_only(path).lower()
        assert not [word for word in _TOOL_VOCABULARY if word in source], _relative(path)


def test_the_run_service_never_reaches_a_tool() -> None:
    """``RunService`` invokes a node and records what it returned. That a node
    held a six-turn conversation with a model is not its business."""

    assert not _violations(
        SRC / "services/run_service.py", ("app.domain.tools", "app.infrastructure.tools")
    )


def test_no_tool_implementation_imports_a_provider() -> None:
    """A tool is Orqent's code, not the framework's.

    The temptation M6 declined: registering a LangChain ``BaseTool`` and letting
    the framework invoke it. That would move execution, argument validation, and
    the allow-list inside the adapter — out of provider-neutral code and out of
    reach of a second provider.
    """

    for package in ("app.domain.tools", "app.infrastructure.tools"):
        for path in _modules(package):
            assert not _ai_imports(path), f"{_relative(path)} reaches a provider"


def test_the_tool_contract_names_no_provider() -> None:
    """The vocabulary crossing this boundary is Orqent's own."""

    for path in _modules("app.domain.tools"):
        source = _code_only(path).lower()
        for vendor in ("gemini", "google", "langchain", "openai", "anthropic", "basetool"):
            assert vendor not in source, f"{_relative(path)} says {vendor}"


def test_provider_tool_translation_lives_only_in_the_llm_adapter() -> None:
    """``bind_tools``, ``AIMessage``, and ``ToolMessage`` are LangChain's
    vocabulary and stop at the adapter."""

    for path in _modules("app"):
        if "app/infrastructure/llm" in path.as_posix():
            continue
        source = _code_only(path)
        for leaked in ("bind_tools", "AIMessage", "ToolMessage", "function_declarations"):
            assert leaked not in source, f"{_relative(path)} uses {leaked}"


def test_no_node_configuration_can_describe_a_tool() -> None:
    """**Names only, catalogue-wide.**

    A config that could carry a schema, an endpoint, or a snippet of code would
    be a config that describes a capability rather than selecting one — which is
    ADR-022's refusal to execute what the catalogue did not ship, restated for
    the thing a *model* can invoke.
    """

    forbidden = {
        "tool_schemas",
        "tool_schema",
        "schema",
        "parameters",
        "endpoint",
        "url",
        "code",
        "script",
        "command",
    }

    for descriptor in build_registry().all():
        config_models = [descriptor.config_model]
        for field in descriptor.config_model.model_fields.values():
            for candidate in getattr(field.annotation, "__args__", ()) or (field.annotation,):
                if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                    config_models.append(candidate)

        for model in config_models:
            leaked = forbidden & set(model.model_fields)
            assert not leaked, f"{descriptor.qualified_name} config exposes {leaked}"


def test_no_tool_accepts_a_tenant_from_the_model() -> None:
    """Tool arguments are written by the model. If one could name an
    organization, a model could redirect a tenant-scoped tool by asking (M5's
    rule, extended to the surface M6 opens)."""

    forbidden = {"organization", "organization_id", "organization_public_id", "tenant", "org_id"}

    for name in CATALOGUE.names():
        fields = set(CATALOGUE.get(name).definition.parameters.model_fields)
        assert not (forbidden & fields), name


def test_every_shipped_tool_is_repeatable() -> None:
    """Execution is at-least-once (ADR-024), so a recovered agent may request the
    same tool again. M6 ships only tools for which that is free, and the registry
    refuses to register anything else."""

    for name in CATALOGUE.names():
        assert CATALOGUE.get(name).definition.side_effect is SideEffect.PURE, name


# =============================================================================
# Backend closure (Phase 10, M7)
# =============================================================================


_NODE_RUNNER_MODULES = tuple(
    str(path.relative_to(SRC))
    for path in _modules("app.infrastructure.nodes.builtin")
    if path.name != "__init__.py"
)

# What a node runner must not reach for. Persistence, the queue, and the HTTP
# layer are all *given* to a node by the engine or held on its behalf; a runner
# that opened its own session would be a runner that could see another tenant's
# rows, escape the transaction the engine settles its result in, or keep working
# after the run was cancelled.
_RUNNER_FORBIDDEN_LAYERS = (
    "app.infrastructure.db.session",
    "app.infrastructure.db.engine",
    "app.infrastructure.db.unit_of_work",
    "app.infrastructure.repositories",
    "app.infrastructure.queue",
    "app.infrastructure.worker",
    "app.infrastructure.dispatcher",
    "app.api",
    "sqlalchemy",
    "fastapi",
)


@pytest.mark.parametrize("module", _NODE_RUNNER_MODULES)
def test_a_node_runner_owns_no_persistence_queue_or_route(module: str) -> None:
    """**The uniform node contract, stated as what a runner cannot reach.**

    ADR-020 says every node is invoked the same way; this says every node is
    *limited* the same way. Phase 10 is where that stopped being obvious: the AI
    runner grew to compose retrieval and tools, and both of those genuinely need
    data — so the tempting shortcut was a session or a repository right here,
    rather than a port handed in at construction.

    Checked across the whole catalogue rather than the AI node alone, because the
    next runner that needs data is the one that would get this wrong.
    """

    assert not _violations(SRC / module, _RUNNER_FORBIDDEN_LAYERS), module


def test_there_are_node_runners_to_check() -> None:
    """Guards the parametrisation above against silently covering nothing."""

    assert len(_NODE_RUNNER_MODULES) >= 8


def _command(service: dict[str, object]) -> str:
    """A Compose ``command`` may be a string or an argv list; both mean the same."""

    command = service["command"]
    return command if isinstance(command, str) else " ".join(str(part) for part in command)


def test_the_local_stack_can_actually_execute_a_run() -> None:
    """**A deployment guard, and it earns its place by having caught a real
    defect.**

    Until M7, `docker-compose.yml` defined `api`, `mysql`, and `chroma` — while
    `project_status.md` described `docker compose up` as the "full stack". It was
    not: with no worker, a run accepted over the API queues in `queue_tasks` and
    never advances, and with no dispatcher a schedule never fires. The stack
    looks healthy and does nothing, which is the worst way for this to be wrong.

    Asserted against the file rather than a running container so it costs
    nothing in CI. It cannot prove the stack *works*; it proves the two processes
    that execute work have not gone missing again.
    """

    compose = yaml.safe_load((SRC.parent.parent / "docker-compose.yml").read_text())
    services = compose["services"]

    assert {"api", "worker", "dispatcher", "mysql", "chroma"} <= set(services)
    assert "app.infrastructure.worker" in _command(services["worker"])
    assert "app.infrastructure.dispatcher" in _command(services["dispatcher"])


def test_only_the_processes_that_use_the_credential_are_given_it() -> None:
    """The worker invokes ``ai.agent@1``; the dispatcher only enqueues.

    A credential in a process that cannot use it is a credential in one more
    environment, one more log, and one more core dump — and configuring *only*
    the API would configure the one process that never calls a provider, which
    is exactly the trap M3 recorded.
    """

    compose = yaml.safe_load((SRC.parent.parent / "docker-compose.yml").read_text())
    services = compose["services"]

    assert "GEMINI_API_KEY" in services["worker"]["environment"]
    assert "APP_CHROMA_HOST" in services["worker"]["environment"]
    assert "GEMINI_API_KEY" not in services["dispatcher"]["environment"]

    # Interpolated from the environment, never written into the file.
    assert services["worker"]["environment"]["GEMINI_API_KEY"].startswith("${")
