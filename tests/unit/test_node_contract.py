"""The node contract (pure domain, no database, no infrastructure).

These assert the invariants that later milestones rely on: graph validation
trusts that a descriptor's handle names are unique, and the catalog API trusts
that descriptors are stable enough to serialise. Both are enforced here, at
construction, rather than checked repeatedly downstream.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from pydantic import BaseModel

from app.domain.errors import AppError
from app.domain.nodes import handles
from app.domain.nodes.descriptor import (
    NodeCategory,
    NodeDescriptor,
    NodeDisplay,
    SideEffect,
)
from app.domain.nodes.handles import Arity, HandleKind, HandleType, InputHandle, OutputHandle
from app.domain.nodes.registry import UnknownNodeTypeError
from app.domain.nodes.result import Completed, Failed, NodeResult, Suspended
from app.domain.nodes.runner import NodeRunContext


class _Config(BaseModel):
    value: str = ""


class _Other(BaseModel):
    other: int = 0


def _descriptor(**overrides: object) -> NodeDescriptor:
    kwargs: dict[str, object] = {
        "node_type": "core.example",
        "version": 1,
        "category": NodeCategory.TRANSFORM,
        "config_model": _Config,
        "display": NodeDisplay(label="Example"),
    }
    kwargs.update(overrides)
    return NodeDescriptor(**kwargs)  # type: ignore[arg-type]


# --- Handle types -----------------------------------------------------------


def test_the_lattice_has_exactly_the_specified_kinds() -> None:
    # Closed by design (ADR-021): growing it is a decision, not an accident.
    assert {kind.value for kind in HandleKind} == {
        "Any",
        "Text",
        "Number",
        "Boolean",
        "Json",
        "Record",
        "Binary",
        "List",
    }


def test_scalar_types_carry_no_parameter() -> None:
    assert handles.TEXT.item is None
    assert handles.TEXT.model is None


def test_list_carries_its_item_type() -> None:
    # Compatibility for lists recurses into this, so it cannot be optional.
    assert handles.list_of(handles.TEXT).item == handles.TEXT


def test_record_carries_its_model() -> None:
    assert handles.record(_Config).model is _Config


@pytest.mark.parametrize(
    "kind",
    [HandleKind.TEXT, HandleKind.JSON, HandleKind.ANY, HandleKind.BINARY],
)
def test_only_list_may_have_an_item_type(kind: HandleKind) -> None:
    with pytest.raises(ValueError, match="List"):
        HandleType(kind, item=handles.TEXT)


def test_list_without_an_item_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="List"):
        HandleType(HandleKind.LIST)


def test_only_record_may_have_a_model() -> None:
    with pytest.raises(ValueError, match="Record"):
        HandleType(HandleKind.TEXT, model=_Config)


def test_record_without_a_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="Record"):
        HandleType(HandleKind.RECORD)


@pytest.mark.parametrize(
    ("handle_type", "expected"),
    [
        (handles.TEXT, "Text"),
        (handles.ANY, "Any"),
        (handles.list_of(handles.TEXT), "List<Text>"),
        (handles.list_of(handles.list_of(handles.NUMBER)), "List<List<Number>>"),
    ],
)
def test_types_render_readably(handle_type: HandleType, expected: str) -> None:
    # These strings end up in validation messages a user has to act on.
    assert str(handle_type) == expected


def test_record_renders_with_its_model_name() -> None:
    assert str(handles.record(_Config)) == "Record<_Config>"


def test_types_are_hashable_and_compare_by_value() -> None:
    assert HandleType(HandleKind.TEXT) == handles.TEXT
    assert len({handles.TEXT, HandleType(HandleKind.TEXT), handles.JSON}) == 2


def test_records_of_different_models_are_not_equal() -> None:
    # Nominal compatibility in Phase 4 depends on exactly this.
    assert handles.record(_Config) != handles.record(_Other)


# --- Handles ----------------------------------------------------------------


def test_input_handle_defaults() -> None:
    handle = InputHandle(name="main", type=handles.TEXT)

    assert handle.arity is Arity.SINGLE
    assert handle.required is True


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_handle_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="blank"):
        InputHandle(name=name, type=handles.TEXT)


def test_overlong_handle_names_are_rejected() -> None:
    # workflow_edges.source_handle is VARCHAR(64); a longer name could not be
    # persisted, so it is refused where it is declared.
    with pytest.raises(ValueError, match="64"):
        OutputHandle(name="x" * 65, type=handles.TEXT)


def test_handle_name_of_exactly_the_limit_is_accepted() -> None:
    assert OutputHandle(name="x" * 64, type=handles.TEXT)


def test_handles_are_frozen() -> None:
    handle = InputHandle(name="main", type=handles.TEXT)

    with pytest.raises(AttributeError):
        handle.name = "other"  # type: ignore[misc]


# --- Descriptor -------------------------------------------------------------


def test_qualified_name() -> None:
    assert _descriptor(node_type="core.constant", version=2).qualified_name == "core.constant@2"


def test_duplicate_input_handle_names_are_rejected() -> None:
    # An edge into a duplicated name would be ambiguous about which socket it
    # meant, and no later validation could recover the intent.
    with pytest.raises(ValueError, match="Duplicate handle name"):
        _descriptor(
            inputs=(
                InputHandle(name="main", type=handles.TEXT),
                InputHandle(name="main", type=handles.JSON),
            )
        )


def test_duplicate_output_handle_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate handle name"):
        _descriptor(
            outputs=(
                OutputHandle(name="main", type=handles.TEXT),
                OutputHandle(name="main", type=handles.JSON),
            )
        )


def test_an_input_and_an_output_may_share_a_name() -> None:
    # Uniqueness is per direction: a pass-through node naturally has `main` on
    # both sides.
    descriptor = _descriptor(
        inputs=(InputHandle(name="main", type=handles.ANY),),
        outputs=(OutputHandle(name="main", type=handles.ANY),),
    )

    assert descriptor.input("main") is not None
    assert descriptor.output("main") is not None


def test_blank_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="blank"):
        _descriptor(node_type="")


@pytest.mark.parametrize("version", [0, -1])
def test_non_positive_versions_are_rejected(version: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        _descriptor(version=version)


def test_handle_lookup_returns_none_when_absent() -> None:
    descriptor = _descriptor(inputs=(InputHandle(name="main", type=handles.TEXT),))

    assert descriptor.input("nope") is None
    assert descriptor.output("main") is None


def test_descriptor_defaults_are_conservative() -> None:
    descriptor = _descriptor()

    assert descriptor.inputs == ()
    assert descriptor.outputs == ()
    assert descriptor.side_effect is SideEffect.PURE
    assert descriptor.deprecated is False


def test_is_trigger() -> None:
    assert _descriptor(category=NodeCategory.TRIGGER).is_trigger is True
    assert _descriptor(category=NodeCategory.OUTPUT).is_trigger is False


def test_descriptors_are_frozen_and_hashable() -> None:
    # Hashable so the catalog and validation may cache and compare them.
    descriptor = _descriptor()

    assert hash(descriptor) == hash(_descriptor())
    with pytest.raises(AttributeError):
        descriptor.version = 2  # type: ignore[misc]


# --- Results ----------------------------------------------------------------


def test_result_variants_are_distinguishable() -> None:
    results: list[NodeResult] = [
        Completed(outputs={"main": "x"}),
        Suspended(resume_token="tok"),
        Failed(error="boom"),
    ]

    assert [type(result).__name__ for result in results] == ["Completed", "Suspended", "Failed"]


def test_completed_defaults_to_no_outputs() -> None:
    # A node that emits nothing is legal — a terminal node does exactly that.
    assert Completed().outputs == {}


def test_suspended_requires_a_resume_token() -> None:
    # The token is what an approval or timer quotes back to resume the run;
    # without it the run could never be woken.
    suspended = Suspended(resume_token="tok")

    assert suspended.resume_token == "tok"
    assert suspended.hint is None


def test_failed_defaults_to_not_retryable() -> None:
    # Retrying is opt-in: assuming a failure is safe to repeat is how a node
    # with side effects duplicates them.
    assert Failed(error="boom").retryable is False


def test_results_are_frozen() -> None:
    result = Completed()

    with pytest.raises(AttributeError):
        result.outputs = {}  # type: ignore[misc]


# --- Runner context ---------------------------------------------------------


def test_run_context_carries_config_and_inputs() -> None:
    context = NodeRunContext(
        config=_Config(value="v"),
        inputs={"main": 1},
        idempotency_key="1:1:1",
        trigger_payload={},
    )

    assert context.config.value == "v"
    assert context.inputs["main"] == 1


def test_unconnected_input_is_absent_rather_than_none() -> None:
    # "not connected" and "connected to null" must stay distinguishable.
    context = NodeRunContext(
        config=_Config(), inputs={}, idempotency_key="1:1:1", trigger_payload={}
    )

    assert "main" not in context.inputs


# --- Registry errors --------------------------------------------------------


def test_unknown_node_type_is_a_domain_error() -> None:
    # It must render through the standard envelope, not escape as a bare
    # exception.
    error = UnknownNodeTypeError()

    assert isinstance(error, AppError)
    assert error.http_status == 500
    assert error.code == "unknown_node_type"


# --- Layering ---------------------------------------------------------------


def test_the_node_contract_imports_no_infrastructure() -> None:
    # Run in a fresh interpreter: another test importing infrastructure would
    # otherwise leave it in sys.modules and hide a real dependency-rule break.
    # Pydantic is deliberately permitted here (ADR-031); frameworks and drivers
    # are not, so this asserts the narrow rule rather than "no third parties".
    program = textwrap.dedent(
        """
        import sys
        import app.domain.nodes.descriptor
        import app.domain.nodes.handles
        import app.domain.nodes.registry
        import app.domain.nodes.result
        import app.domain.nodes.runner

        forbidden = {"fastapi", "sqlalchemy", "starlette", "asyncmy", "alembic", "jwt", "argon2"}
        leaked = sorted(m for m in sys.modules if m.split(".")[0] in forbidden)
        app_leaks = sorted(
            m for m in sys.modules
            if m.startswith(("app.infrastructure", "app.services", "app.api"))
        )
        print("|".join(leaked + app_leaks))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == ""
