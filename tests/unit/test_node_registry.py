"""The node registry and the built-in catalogue (no database, no config).

The conformance suite at the bottom is the important part: it is parametrized
over ``registry.all()``, so every node type added in any later phase is held to
these rules the moment it is registered. That is what keeps "adding a node
touches nothing else" true by construction rather than by review.
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay
from app.domain.nodes.registry import NodeRegistry, UnknownNodeTypeError
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin import (
    core_constant,
    core_log,
    core_noop,
    trigger_manual,
    trigger_schedule,
    trigger_webhook,
)
from app.infrastructure.nodes.registry import DuplicateNodeTypeError, InMemoryNodeRegistry

BUILT_IN_NAMES = (
    "trigger.manual@1",
    "trigger.webhook@1",
    "trigger.schedule@1",
    "core.constant@1",
    "core.noop@1",
    "core.log@1",
    "core.wait@1",
    "core.condition@1",
    "core.merge@1",
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


def test_webhook_trigger_shape() -> None:
    descriptor = trigger_webhook.DESCRIPTOR

    assert descriptor.is_trigger
    assert descriptor.inputs == ()  # a trigger starts the graph
    assert descriptor.output("main") is not None
    # The same type the manual trigger emits, so everything already connected
    # downstream of a trigger accepts a webhook one unchanged.
    assert descriptor.output("main").type == handles.JSON  # type: ignore[union-attr]


def test_a_webhook_trigger_has_nothing_to_configure() -> None:
    """The address is the platform's to mint, not the author's to choose.

    A user-selected token is a guessable token, and an unauthenticated receiver
    has nothing else protecting it — so there is deliberately no field here to
    put one in. ``extra="forbid"`` is what makes that a refusal rather than a
    silently ignored key.
    """

    assert trigger_webhook.WebhookTriggerConfig().model_dump() == {}
    with pytest.raises(ValidationError):
        trigger_webhook.WebhookTriggerConfig(token="guess-me")  # type: ignore[call-arg]


def test_the_two_triggers_are_distinct_node_types() -> None:
    """Same behaviour today, different identities — which is the whole point:
    the type is what M2's registration and M4's receiver key off."""

    assert trigger_webhook.DESCRIPTOR.node_type != trigger_manual.DESCRIPTOR.node_type
    assert trigger_webhook.DESCRIPTOR.category is trigger_manual.DESCRIPTOR.category


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


def test_the_built_in_triggers_are_the_ones_we_meant(registry: NodeRegistry) -> None:
    # Pinned so a new trigger type is a *deliberate* addition, exactly as the
    # single-trigger version of this test intended. `trigger.webhook@1` arrived
    # in Phase 9 M1 and `trigger.schedule@1` in M5; a workflow still declares
    # exactly one of them.
    triggers = [d.qualified_name for d in registry.all() if d.is_trigger]
    assert triggers == ["trigger.manual@1", "trigger.webhook@1", "trigger.schedule@1"]


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


async def test_webhook_trigger_hands_over_the_payload_unchanged() -> None:
    """A trigger carries data into the graph; it does not interpret it."""

    payload = {"order": 7, "nested": {"deep": True}}
    result = await trigger_webhook.RUNNER.run(
        NodeRunContext(
            config=trigger_webhook.WebhookTriggerConfig(),
            inputs={},
            idempotency_key="1:1:1",
            trigger_payload=payload,
        )
    )

    assert isinstance(result, Completed)
    assert result.outputs["main"] == payload


async def test_schedule_trigger_hands_over_the_payload_unchanged() -> None:
    """Like every trigger, it carries data in rather than deciding anything.

    In particular it consults no clock. By the time this runs, the schedule has
    already fired and a run already exists — the runner is the ordinary first
    node of an ordinary run, which is why it is deterministic and testable with
    no database, no queue, and no time at all.
    """

    payload = {"fired_at": "2026-08-19T00:00:00Z"}
    result = await trigger_schedule.RUNNER.run(
        NodeRunContext(
            config=trigger_schedule.ScheduleTriggerConfig(),
            inputs={},
            idempotency_key="1:1:1",
            trigger_payload=payload,
        )
    )

    assert isinstance(result, Completed)
    assert result.outputs["main"] == payload


async def test_the_schedule_runner_is_deterministic() -> None:
    """Two invocations of the same context give the same answer.

    Worth pinning rather than assuming: a scheduling node is exactly where a
    ``datetime.now()`` would be tempting, and at-least-once delivery means this
    runner *will* sometimes be invoked twice for one firing (ADR-024). If it read
    a clock, the two attempts would disagree about what the run was started with.
    """

    context = NodeRunContext(
        config=trigger_schedule.ScheduleTriggerConfig(),
        inputs={},
        idempotency_key="1:1:1",
        trigger_payload={"n": 1},
    )

    first = await trigger_schedule.RUNNER.run(context)
    second = await trigger_schedule.RUNNER.run(context)

    assert isinstance(first, Completed)
    assert isinstance(second, Completed)
    assert first.outputs == second.outputs


async def test_a_webhook_trigger_started_with_no_payload_emits_none() -> None:
    """`None` is distinct from `{}`: "started with nothing" and "started with an
    empty object" are different facts, and the run row stores them differently."""

    result = await trigger_webhook.RUNNER.run(
        NodeRunContext(
            config=trigger_webhook.WebhookTriggerConfig(),
            inputs={},
            idempotency_key="1:1:1",
            trigger_payload=None,
        )
    )

    assert isinstance(result, Completed)
    assert result.outputs["main"] is None


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
