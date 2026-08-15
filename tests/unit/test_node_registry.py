"""The node registry and the built-in catalogue (no database, no config).

The conformance suite at the bottom is the important part: it is parametrized
over ``registry.all()``, so every node type added in any later phase is held to
these rules the moment it is registered. That is what keeps "adding a node
touches nothing else" true by construction rather than by review.
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, ConfigDict

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay
from app.domain.nodes.registry import NodeRegistry, UnknownNodeTypeError
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin import core_constant, core_log, core_noop, trigger_manual
from app.infrastructure.nodes.registry import DuplicateNodeTypeError, InMemoryNodeRegistry

BUILT_IN_NAMES = (
    "trigger.manual@1",
    "core.constant@1",
    "core.noop@1",
    "core.log@1",
    "core.wait@1",
)

# `namespace.name`, lower snake case. Keeps the catalogue readable and the
# qualified name safe to embed in JSON, URLs, and validation messages.
NODE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Runner(NodeRunner):
    async def run(self, context: NodeRunContext) -> NodeResult:
        return Completed()


def _descriptor(node_type: str = "test.example", version: int = 1) -> NodeDescriptor:
    return NodeDescriptor(
        node_type=node_type,
        version=version,
        category=NodeCategory.TRANSFORM,
        config_model=_Config,
        display=NodeDisplay(label="Example"),
    )


@pytest.fixture(scope="module")
def registry() -> NodeRegistry:
    return build_registry()


# --- Registry mechanics -----------------------------------------------------


def test_register_then_lookup() -> None:
    subject = InMemoryNodeRegistry()
    descriptor = _descriptor()

    subject.register(descriptor, _Runner())

    assert subject.get("test.example", 1) is descriptor


def test_get_raises_for_an_unknown_type() -> None:
    with pytest.raises(UnknownNodeTypeError):
        InMemoryNodeRegistry().get("test.absent", 1)


def test_find_returns_none_for_an_unknown_type() -> None:
    # Validation expects misses, so it must not pay for exception handling in
    # the ordinary case.
    assert InMemoryNodeRegistry().find("test.absent", 1) is None


def test_version_is_part_of_the_identity() -> None:
    subject = InMemoryNodeRegistry()
    subject.register(_descriptor(version=1), _Runner())

    assert subject.find("test.example", 1) is not None
    assert subject.find("test.example", 2) is None


def test_two_versions_of_one_type_coexist() -> None:
    # A breaking change ships as a new version while the old one keeps working
    # for already-published workflows (ADR-022).
    subject = InMemoryNodeRegistry()
    subject.register(_descriptor(version=1), _Runner())
    subject.register(_descriptor(version=2), _Runner())

    assert subject.get("test.example", 1).version == 1
    assert subject.get("test.example", 2).version == 2


def test_duplicate_registration_raises() -> None:
    # A programming error caught while assembling the catalogue; the alternative
    # is one node silently shadowing another.
    subject = InMemoryNodeRegistry()
    subject.register(_descriptor(), _Runner())

    with pytest.raises(DuplicateNodeTypeError, match="already registered"):
        subject.register(_descriptor(), _Runner())


def test_runner_lookup() -> None:
    subject = InMemoryNodeRegistry()
    runner = _Runner()
    subject.register(_descriptor(), runner)

    assert subject.runner("test.example", 1) is runner


def test_runner_raises_for_an_unknown_type() -> None:
    with pytest.raises(UnknownNodeTypeError):
        InMemoryNodeRegistry().runner("test.absent", 1)


def test_all_is_empty_for_a_fresh_registry() -> None:
    assert InMemoryNodeRegistry().all() == ()


def test_all_preserves_registration_order() -> None:
    # The catalog API serialises this directly; a shifting order would churn
    # frontend snapshots for no reason.
    subject = InMemoryNodeRegistry()
    subject.register(_descriptor(node_type="test.c"), _Runner())
    subject.register(_descriptor(node_type="test.a"), _Runner())
    subject.register(_descriptor(node_type="test.b"), _Runner())

    assert [d.node_type for d in subject.all()] == ["test.c", "test.a", "test.b"]


# --- The built-in catalogue -------------------------------------------------


def test_build_registry_needs_no_database_or_settings(registry: NodeRegistry) -> None:
    # Assembling the catalogue is pure, which is why the container can hold it
    # and every test can rebuild it in a line.
    assert registry.all()


def test_every_built_in_is_registered(registry: NodeRegistry) -> None:
    assert [d.qualified_name for d in registry.all()] == list(BUILT_IN_NAMES)


def test_building_twice_yields_equivalent_catalogues() -> None:
    assert [d.qualified_name for d in build_registry().all()] == [
        d.qualified_name for d in build_registry().all()
    ]


def test_manual_trigger_shape() -> None:
    descriptor = trigger_manual.DESCRIPTOR

    assert descriptor.is_trigger
    assert descriptor.inputs == ()  # a trigger starts the graph
    assert descriptor.output("main") is not None
    assert descriptor.output("main").type == handles.JSON  # type: ignore[union-attr]


def test_constant_emits_text() -> None:
    # Text rather than Json is what makes constant -> log a legal connection.
    assert core_constant.DESCRIPTOR.output("main").type == handles.TEXT  # type: ignore[union-attr]


def test_noop_is_any_in_and_any_out() -> None:
    descriptor = core_noop.DESCRIPTOR

    assert descriptor.input("main").type == handles.ANY  # type: ignore[union-attr]
    assert descriptor.output("main").type == handles.ANY  # type: ignore[union-attr]


def test_log_is_terminal_and_takes_text() -> None:
    descriptor = core_log.DESCRIPTOR

    assert descriptor.outputs == ()  # terminal: nothing may follow it
    assert descriptor.input("main").type == handles.TEXT  # type: ignore[union-attr]


def test_the_catalogue_spans_the_incompatibility_case() -> None:
    # The pair M6 will use to prove type checking works at all: Json cannot
    # reach a Text input, but Text can.
    assert trigger_manual.DESCRIPTOR.output("main").type == handles.JSON  # type: ignore[union-attr]
    assert core_log.DESCRIPTOR.input("main").type == handles.TEXT  # type: ignore[union-attr]


def test_exactly_one_built_in_is_a_trigger(registry: NodeRegistry) -> None:
    # Workflows require exactly one trigger node, so a second trigger type would
    # not break anything — but today there is one, and the count is worth
    # pinning so a new one is a deliberate addition.
    assert sum(1 for d in registry.all() if d.is_trigger) == 1


# --- Runners ----------------------------------------------------------------


async def test_manual_trigger_completes_on_its_output_handle() -> None:
    result = await trigger_manual.RUNNER.run(
        NodeRunContext(
            config=trigger_manual.ManualTriggerConfig(),
            inputs={},
            idempotency_key="1:1:1",
            trigger_payload={},
        )
    )

    assert isinstance(result, Completed)
    assert "main" in result.outputs


async def test_constant_returns_its_configured_value() -> None:
    result = await core_constant.RUNNER.run(
        NodeRunContext(
            config=core_constant.ConstantConfig(value="hello"),
            inputs={},
            idempotency_key="1:1:1",
            trigger_payload={},
        )
    )

    assert isinstance(result, Completed)
    assert result.outputs["main"] == "hello"


async def test_noop_passes_its_input_through() -> None:
    payload = {"anything": [1, 2, 3]}

    result = await core_noop.RUNNER.run(
        NodeRunContext(
            config=core_noop.NoOpConfig(),
            inputs={"main": payload},
            idempotency_key="1:1:1",
            trigger_payload={},
        )
    )

    assert isinstance(result, Completed)
    assert result.outputs["main"] is payload


async def test_log_completes_with_no_outputs() -> None:
    result = await core_log.RUNNER.run(
        NodeRunContext(
            config=core_log.LogConfig(),
            inputs={"main": "hello"},
            idempotency_key="1:1:1",
            trigger_payload={},
        )
    )

    assert isinstance(result, Completed)
    assert result.outputs == {}


# --- Config models ----------------------------------------------------------


def test_constant_rejects_an_overlong_value() -> None:
    with pytest.raises(ValueError, match="at most"):
        core_constant.ConstantConfig(value="x" * (core_constant.MAX_VALUE_LENGTH + 1))


def test_log_level_defaults_to_info() -> None:
    assert core_log.LogConfig().level is core_log.LogLevel.INFO


def test_log_rejects_an_unknown_level() -> None:
    with pytest.raises(ValueError, match="level"):
        core_log.LogConfig(level="verbose")  # type: ignore[arg-type]


# --- Conformance: every registered node type, now and in future --------------


def _all_descriptors() -> list[NodeDescriptor]:
    return list(build_registry().all())


def _ids(descriptors: list[NodeDescriptor]) -> list[str]:
    return [d.qualified_name for d in descriptors]


CATALOGUE = _all_descriptors()


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_node_type_naming(descriptor: NodeDescriptor) -> None:
    assert NODE_TYPE_PATTERN.match(descriptor.node_type), descriptor.node_type


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_version_is_positive(descriptor: NodeDescriptor) -> None:
    assert descriptor.version >= 1


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_has_a_display_label(descriptor: NodeDescriptor) -> None:
    # The palette renders from this; an unnamed node is unusable.
    assert descriptor.display.label.strip()
    assert descriptor.display.description.strip()


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_config_model_is_a_pydantic_model(descriptor: NodeDescriptor) -> None:
    # The catalog API publishes JSON Schema generated from this.
    assert issubclass(descriptor.config_model, BaseModel)
    assert descriptor.config_model.model_json_schema()


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_config_forbids_unknown_keys(descriptor: NodeDescriptor) -> None:
    # Enforced here rather than by a shared base class, so each node module
    # stays self-contained while the policy still holds catalogue-wide.
    assert descriptor.config_model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_config_model_is_constructible_with_defaults(
    descriptor: NodeDescriptor,
) -> None:
    # A node dropped onto the canvas has no configuration yet, so every field
    # must default. A required field would make a fresh node invalid on arrival.
    assert descriptor.config_model()


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_handle_names_are_unique_per_direction(
    descriptor: NodeDescriptor,
) -> None:
    assert len({h.name for h in descriptor.inputs}) == len(descriptor.inputs)
    assert len({h.name for h in descriptor.outputs}) == len(descriptor.outputs)


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_triggers_have_no_inputs(descriptor: NodeDescriptor) -> None:
    # A trigger starts the graph; an inbound edge would make it unreachable
    # from itself.
    if descriptor.is_trigger:
        assert descriptor.inputs == ()


@pytest.mark.parametrize("descriptor", CATALOGUE, ids=_ids(CATALOGUE))
def test_conformance_has_a_runner(descriptor: NodeDescriptor) -> None:
    runner = build_registry().runner(descriptor.node_type, descriptor.version)

    assert isinstance(runner, NodeRunner)
