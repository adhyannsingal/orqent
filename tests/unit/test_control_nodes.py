"""`core.condition@1` and `core.merge@1` (Phase 7, M4).

Two ordinary nodes. Nothing here reaches into the engine, and nothing in the
engine reaches into these — the branching they enable is a consequence of which
handle produced a value, which `test_engine_conformance.py` proves separately.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.nodes.descriptor import NodeCategory, SideEffect
from app.domain.nodes.result import Completed, NodeResult
from app.domain.nodes.runner import NodeRunContext
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin import core_condition, core_merge
from app.infrastructure.nodes.builtin.core_condition import ConditionConfig, Operator

TRUE, FALSE = core_condition.TRUE_HANDLE, core_condition.FALSE_HANDLE
A, B = core_merge.FIRST_HANDLE, core_merge.SECOND_HANDLE


async def _decide(config: ConditionConfig, incoming: object = None) -> NodeResult:
    return await core_condition.RUNNER.run(
        NodeRunContext(
            config=config,
            inputs={"main": incoming} if incoming is not None else {},
            idempotency_key="1:1:1",
            trigger_payload={},
        )
    )


async def _taken(config: ConditionConfig, incoming: object = None) -> str:
    """Which handle the condition emitted on."""

    result = await _decide(config, incoming)
    assert isinstance(result, Completed)
    assert len(result.outputs) == 1, f"expected exactly one handle, got {sorted(result.outputs)}"
    return next(iter(result.outputs))


async def _merge(inputs: dict[str, Any]) -> NodeResult:
    return await core_merge.RUNNER.run(
        NodeRunContext(
            config=core_merge.MergeConfig(),
            inputs=inputs,
            idempotency_key="1:1:1",
            trigger_payload={},
        )
    )


# --- Registration ------------------------------------------------------------


@pytest.mark.parametrize("qualified", ["core.condition@1", "core.merge@1"])
def test_both_nodes_are_in_the_catalogue(qualified: str) -> None:
    assert qualified in [d.qualified_name for d in build_registry().all()]


@pytest.mark.parametrize(("node_type", "version"), [("core.condition", 1), ("core.merge", 1)])
def test_both_nodes_resolve_to_a_runner(node_type: str, version: int) -> None:
    """Discovered exactly like every other built-in — the engine resolves them
    through the port and never imports them."""

    assert build_registry().runner(node_type, version) is not None


def test_the_condition_declares_two_output_handles() -> None:
    descriptor = build_registry().get("core.condition", 1)

    assert [handle.name for handle in descriptor.outputs] == [TRUE, FALSE]
    assert descriptor.side_effect is SideEffect.PURE
    assert descriptor.category is NodeCategory.CONTROL


def test_the_merge_declares_two_optional_input_handles() -> None:
    """Optional because only one branch arrives — requiring either would make
    validation demand an edge on a branch that may legitimately be pruned."""

    descriptor = build_registry().get("core.merge", 1)

    assert [handle.name for handle in descriptor.inputs] == [A, B]
    assert all(not handle.required for handle in descriptor.inputs)
    assert [handle.name for handle in descriptor.outputs] == ["main"]


# --- Condition: configuration ------------------------------------------------


def test_a_valid_configuration_is_accepted() -> None:
    config = ConditionConfig(path="customer.tier", operator=Operator.EQUALS, value="gold")

    assert config.path == "customer.tier"
    assert config.operator is Operator.EQUALS


def test_an_unknown_field_is_rejected() -> None:
    """`extra="forbid"`, like every other node's config — a typo in a workflow
    definition is a validation error at publish, not a silent no-op at run."""

    with pytest.raises(ValidationError):
        ConditionConfig(path="a", operator=Operator.EQUALS, valu="typo")  # type: ignore[call-arg]


def test_an_unknown_operator_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConditionConfig(operator="approximately")  # type: ignore[arg-type]


def test_an_overlong_path_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConditionConfig(path="x" * (core_condition.MAX_PATH_LENGTH + 1))


# --- Condition: exactly one handle -------------------------------------------


@pytest.mark.parametrize(
    ("operator", "value", "incoming", "expected"),
    [
        (Operator.EQUALS, "gold", {"tier": "gold"}, TRUE),
        (Operator.EQUALS, "gold", {"tier": "silver"}, FALSE),
        (Operator.NOT_EQUALS, "gold", {"tier": "silver"}, TRUE),
        (Operator.NOT_EQUALS, "gold", {"tier": "gold"}, FALSE),
        (Operator.GREATER_THAN, 10, {"tier": 11}, TRUE),
        (Operator.GREATER_THAN, 10, {"tier": 10}, FALSE),
        (Operator.LESS_THAN, 10, {"tier": 9}, TRUE),
        (Operator.LESS_THAN, 10, {"tier": 10}, FALSE),
        (Operator.CONTAINS, "ol", {"tier": "gold"}, TRUE),
        (Operator.CONTAINS, "zz", {"tier": "gold"}, FALSE),
        (Operator.IS_EMPTY, None, {"tier": ""}, TRUE),
        (Operator.IS_EMPTY, None, {"tier": "gold"}, FALSE),
    ],
)
async def test_each_operator_picks_the_expected_handle(
    operator: Operator, value: object, incoming: dict[str, object], expected: str
) -> None:
    taken = await _taken(ConditionConfig(path="tier", operator=operator, value=value), incoming)

    assert taken == expected


async def test_the_condition_never_emits_both_handles() -> None:
    result = await _decide(
        ConditionConfig(path="tier", operator=Operator.EQUALS, value="gold"), {"tier": "gold"}
    )

    assert isinstance(result, Completed)
    assert set(result.outputs) == {TRUE}
    assert FALSE not in result.outputs


async def test_the_condition_never_emits_neither_handle() -> None:
    """Emitting nothing would prune both branches and strand the run."""

    for incoming in ({"tier": "gold"}, {"other": 1}, {}, None):
        result = await _decide(
            ConditionConfig(path="tier", operator=Operator.EQUALS, value="gold"), incoming
        )
        assert isinstance(result, Completed)
        assert len(result.outputs) == 1


async def test_the_same_input_always_takes_the_same_path() -> None:
    config = ConditionConfig(path="tier", operator=Operator.EQUALS, value="gold")

    first = await _taken(config, {"tier": "gold"})
    second = await _taken(config, {"tier": "gold"})

    assert first == second == TRUE


async def test_the_incoming_value_is_forwarded_on_the_taken_handle() -> None:
    """So the branch can keep working with what arrived."""

    payload = {"tier": "gold", "id": 7}
    result = await _decide(
        ConditionConfig(path="tier", operator=Operator.EQUALS, value="gold"), payload
    )

    assert isinstance(result, Completed)
    assert result.outputs[TRUE] == payload


# --- Condition: the predicate is total ---------------------------------------


async def test_an_unresolvable_path_is_false_rather_than_an_error() -> None:
    """A branch is a decision, not a place to discover a typo — failing here
    would take the whole run down over a field that simply was not there."""

    taken = await _taken(
        ConditionConfig(path="a.b.c", operator=Operator.EQUALS, value=1), {"a": {"x": 1}}
    )

    assert taken == FALSE


async def test_an_empty_path_examines_the_whole_value() -> None:
    taken = await _taken(ConditionConfig(operator=Operator.EQUALS, value="gold"), "gold")

    assert taken == TRUE


@pytest.mark.parametrize("subject", [None, "", [], {}, ()])
async def test_is_empty_recognises_every_kind_of_nothing(subject: object) -> None:
    taken = await _taken(ConditionConfig(path="v", operator=Operator.IS_EMPTY), {"v": subject})

    assert taken == TRUE


async def test_comparing_a_string_to_a_number_is_false_not_a_crash() -> None:
    taken = await _taken(
        ConditionConfig(path="v", operator=Operator.GREATER_THAN, value=10), {"v": "gold"}
    )

    assert taken == FALSE


async def test_a_boolean_is_not_treated_as_a_number() -> None:
    """Python says `True > 0`; an author comparing a flag to a number has made
    a mistake the engine should not quietly ratify."""

    taken = await _taken(
        ConditionConfig(path="v", operator=Operator.GREATER_THAN, value=0), {"v": True}
    )

    assert taken == FALSE


async def test_the_path_walks_only_mappings() -> None:
    """No attribute access: that would reach into whatever object a future node
    happens to return."""

    taken = await _taken(
        ConditionConfig(path="v.real", operator=Operator.EQUALS, value=1), {"v": 1}
    )

    assert taken == FALSE


# --- Merge -------------------------------------------------------------------


async def test_the_merge_forwards_the_first_branch() -> None:
    result = await _merge({A: {"from": "left"}})

    assert result == Completed(outputs={"main": {"from": "left"}})


async def test_the_merge_forwards_the_second_branch() -> None:
    result = await _merge({B: {"from": "right"}})

    assert result == Completed(outputs={"main": {"from": "right"}})


async def test_the_first_handle_wins_when_both_arrive() -> None:
    """A documented tie-break rather than an ordering that depends on which
    branch happened to finish first. Both arriving is a parallel fan-in, whose
    join policy is a later phase — but the answer has to be stated."""

    result = await _merge({A: "left", B: "right"})

    assert result == Completed(outputs={"main": "left"})


async def test_the_merge_is_deterministic() -> None:
    assert await _merge({A: "left", B: "right"}) == await _merge({A: "left", B: "right"})


async def test_the_merge_forwards_an_explicit_none() -> None:
    """ "Arrived carrying null" is not the same as "did not arrive"."""

    result = await _merge({B: None})

    assert result == Completed(outputs={"main": None})


async def test_the_merge_emits_nothing_when_no_branch_arrived() -> None:
    """Unreachable through the engine — the scheduler prunes a node whose every
    inbound edge is dead. Emitting nothing rather than a fabricated value keeps
    that true: downstream is pruned instead of running on a lie."""

    result = await _merge({})

    assert result == Completed()
