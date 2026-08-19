"""Node invocation (Phase 6, M6) — pure, no database.

Input resolution, key derivation, context assembly, and result mapping. The
registry is the real ``InMemoryNodeRegistry`` with the real built-ins: it is a
pure in-process map, so using it here tests the actual contract rather than a
double's impression of it.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from app.domain.engine.invocation import build_context, idempotency_key, invoke, resolve_inputs
from app.domain.engine.snapshot import NodeExecutionSnapshot, RunSnapshot
from app.domain.engine.state import NodeExecutionStatus, RunStatus
from app.domain.errors import DomainRuleError
from app.domain.graph.model import GraphEdge, GraphNode, WorkflowGraph
from app.domain.nodes import handles
from app.domain.nodes.descriptor import NodeCategory, NodeDescriptor, NodeDisplay, SideEffect
from app.domain.nodes.handles import InputHandle, OutputHandle
from app.domain.nodes.registry import NodeRegistry
from app.domain.nodes.result import Completed, Failed, NodeResult, Suspended
from app.domain.nodes.runner import NodeRunContext, NodeRunner
from app.infrastructure.db.identifiers import PUBLIC_ID_LENGTH
from app.infrastructure.nodes import build_registry
from app.infrastructure.nodes.builtin import core_wait
from app.infrastructure.nodes.registry import InMemoryNodeRegistry

SUCCEEDED = NodeExecutionStatus.SUCCEEDED
PENDING = NodeExecutionStatus.PENDING


def _snapshot(
    nodes: tuple[GraphNode, ...],
    edges: tuple[GraphEdge, ...],
    outputs: dict[str, dict[str, object] | None],
    *,
    trigger_payload: dict[str, object] | None = None,
) -> RunSnapshot:
    return RunSnapshot(
        status=RunStatus.RUNNING,
        graph=WorkflowGraph(nodes=nodes, edges=edges),
        node_executions={
            node.key: NodeExecutionSnapshot(
                node_key=node.key,
                status=SUCCEEDED if outputs.get(node.key) is not None else PENDING,
                attempt=1,
                outputs=outputs.get(node.key),
            )
            for node in nodes
        },
        trigger_payload=trigger_payload,
    )


def _node(key: str, node_type: str = "core.noop") -> GraphNode:
    return GraphNode(key=key, node_type=node_type, version=1)


def _edge(
    source: str, target: str, *, source_handle: str = "main", target_handle: str = "main"
) -> GraphEdge:
    return GraphEdge(
        source_key=source,
        source_handle=source_handle,
        target_key=target,
        target_handle=target_handle,
    )


# --- Input resolution -------------------------------------------------------


def test_a_single_upstream_output_becomes_the_downstream_input() -> None:
    snapshot = _snapshot((_node("a"), _node("b")), (_edge("a", "b"),), {"a": {"main": "hello"}})

    assert resolve_inputs(snapshot, "b") == {"main": "hello"}


def test_handles_are_renamed_across_the_edge() -> None:
    """The edge says which socket feeds which; the names need not match."""

    snapshot = _snapshot(
        (_node("a"), _node("b")),
        (_edge("a", "b", source_handle="out", target_handle="in"),),
        {"a": {"out": 1}},
    )

    assert resolve_inputs(snapshot, "b") == {"in": 1}


def test_two_upstreams_fill_two_input_handles() -> None:
    snapshot = _snapshot(
        (_node("left"), _node("right"), _node("join")),
        (
            _edge("left", "join", target_handle="first"),
            _edge("right", "join", target_handle="second"),
        ),
        {"left": {"main": 1}, "right": {"main": 2}},
    )

    assert resolve_inputs(snapshot, "join") == {"first": 1, "second": 2}


def test_an_upstream_handle_that_produced_nothing_leaves_the_input_absent() -> None:
    """Not ``None``: "not connected" and "connected to null" must stay
    distinguishable, which is how a conditional output stays silent."""

    snapshot = _snapshot((_node("a"), _node("b")), (_edge("a", "b"),), {"a": {"other": "value"}})

    assert resolve_inputs(snapshot, "b") == {}
    assert "main" not in resolve_inputs(snapshot, "b")


def test_an_upstream_that_produced_an_explicit_none_still_delivers_it() -> None:
    """The other half of the distinction: a handle that emitted ``None``
    genuinely emitted something."""

    snapshot = _snapshot((_node("a"), _node("b")), (_edge("a", "b"),), {"a": {"main": None}})

    assert resolve_inputs(snapshot, "b") == {"main": None}


def test_a_node_with_no_inbound_edges_receives_no_inputs() -> None:
    snapshot = _snapshot((_node("solo"),), (), {})

    assert resolve_inputs(snapshot, "solo") == {}


def test_an_upstream_with_no_outputs_at_all_is_skipped() -> None:
    snapshot = _snapshot((_node("a"), _node("b")), (_edge("a", "b"),), {"a": None})

    assert resolve_inputs(snapshot, "b") == {}


@pytest.mark.parametrize(
    "value", [1, 1.5, "text", True, None, {"nested": [1, 2]}, [1, "two", {"three": 3}]]
)
def test_values_cross_the_edge_unchanged(value: object) -> None:
    """No coercion: the type lattice settled compatibility at authoring time."""

    snapshot = _snapshot((_node("a"), _node("b")), (_edge("a", "b"),), {"a": {"main": value}})

    assert resolve_inputs(snapshot, "b")["main"] is value


# --- Idempotency key --------------------------------------------------------


def test_the_key_is_built_from_run_node_and_attempt() -> None:
    assert idempotency_key(7, 42, 3) == "7:42:3"


def test_the_same_attempt_yields_the_same_key() -> None:
    assert idempotency_key(1, 2, 1) == idempotency_key(1, 2, 1)


def test_a_later_attempt_yields_a_different_key() -> None:
    """What lets a node recognise its own earlier call rather than its retry."""

    assert idempotency_key(1, 2, 1) != idempotency_key(1, 2, 2)


def test_different_nodes_of_one_run_do_not_share_a_key() -> None:
    assert idempotency_key(1, 2, 1) != idempotency_key(1, 3, 1)


def test_the_same_node_in_two_runs_does_not_share_a_key() -> None:
    assert idempotency_key(1, 2, 1) != idempotency_key(2, 2, 1)


# --- Context assembly -------------------------------------------------------


def _registry() -> NodeRegistry:
    return build_registry()


def test_the_context_carries_the_validated_config_model() -> None:
    """A runner is promised a model, never raw JSON."""

    snapshot = _snapshot(
        (GraphNode(key="c", node_type="core.constant", version=1, config={"value": "hi"}),), (), {}
    )

    context = build_context(
        snapshot,
        _registry(),
        "c",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=2,
        attempt=1,
    )

    assert isinstance(context.config, BaseModel)
    assert context.config.value == "hi"  # type: ignore[attr-defined]


def test_the_context_carries_the_idempotency_key() -> None:
    snapshot = _snapshot((GraphNode(key="t", node_type="trigger.manual", version=1),), (), {})

    context = build_context(
        snapshot,
        _registry(),
        "t",
        run_id=9,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=8,
        attempt=2,
    )

    assert context.idempotency_key == "9:8:2"


def test_the_context_carries_the_trigger_payload() -> None:
    snapshot = _snapshot(
        (GraphNode(key="t", node_type="trigger.manual", version=1),),
        (),
        {},
        trigger_payload={"order": 7},
    )

    context = build_context(
        snapshot,
        _registry(),
        "t",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=1,
        attempt=1,
    )

    assert context.trigger_payload == {"order": 7}


def test_an_absent_trigger_payload_becomes_an_empty_mapping() -> None:
    """A run started with nothing must not hand a node ``None``."""

    snapshot = _snapshot((GraphNode(key="t", node_type="trigger.manual", version=1),), (), {})

    context = build_context(
        snapshot,
        _registry(),
        "t",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=1,
        attempt=1,
    )

    assert context.trigger_payload == {}


def test_every_node_receives_the_payload_not_only_the_trigger() -> None:
    """Which is what keeps the engine from knowing what a trigger is."""

    snapshot = _snapshot(
        (GraphNode(key="n", node_type="core.noop", version=1),),
        (),
        {},
        trigger_payload={"a": 1},
    )

    context = build_context(
        snapshot,
        _registry(),
        "n",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=1,
        attempt=1,
    )

    assert context.trigger_payload == {"a": 1}


def test_an_unknown_node_key_is_refused() -> None:
    snapshot = _snapshot((_node("a"),), (), {})

    with pytest.raises(DomainRuleError, match="no node 'ghost'"):
        build_context(
            snapshot,
            _registry(),
            "ghost",
            run_id=1,
            organization_public_id="01ORGORGORGORGORGORGORGORG",
            workflow_node_id=1,
            attempt=1,
        )


def test_config_the_node_type_no_longer_accepts_is_reported_against_the_node() -> None:
    """Publishing validated it, so divergence means the graph and the registry
    disagree — an append-only violation or stale code."""

    snapshot = _snapshot(
        (GraphNode(key="c", node_type="core.constant", version=1, config={"nope": 1}),), (), {}
    )

    with pytest.raises(DomainRuleError, match="no longer accepts"):
        build_context(
            snapshot,
            _registry(),
            "c",
            run_id=1,
            organization_public_id="01ORGORGORGORGORGORGORGORG",
            workflow_node_id=1,
            attempt=1,
        )


# --- Invocation -------------------------------------------------------------


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _custom(runner: NodeRunner, node_type: str = "test.node") -> NodeRegistry:
    registry = InMemoryNodeRegistry()
    registry.register(
        NodeDescriptor(
            node_type=node_type,
            version=1,
            category=NodeCategory.ACTION,
            config_model=_Config,
            display=NodeDisplay(label="Test", description="A test node.", icon="x"),
            inputs=(InputHandle(name="main", type=handles.ANY, required=False),),
            outputs=(OutputHandle(name="main", type=handles.ANY),),
            side_effect=SideEffect.PURE,
        ),
        runner,
    )
    return registry


def _context() -> NodeRunContext:
    return NodeRunContext(
        config=_Config(),
        inputs={},
        idempotency_key="1:1:1",
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        trigger_payload={},
    )


async def test_a_completed_result_is_returned_unchanged() -> None:
    class _Ok(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            return Completed(outputs={"main": 42})

    registry = _custom(_Ok())
    snapshot = _snapshot((GraphNode(key="n", node_type="test.node", version=1),), (), {})

    result = await invoke(snapshot, registry, "n", _context())

    assert result == Completed(outputs={"main": 42})


async def test_a_failed_result_is_returned_unchanged() -> None:
    class _No(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            return Failed(error="upstream refused", retryable=True)

    registry = _custom(_No())
    snapshot = _snapshot((GraphNode(key="n", node_type="test.node", version=1),), (), {})

    result = await invoke(snapshot, registry, "n", _context())

    assert result == Failed(error="upstream refused", retryable=True)


async def test_an_escaping_exception_becomes_a_non_retryable_failure() -> None:
    """``NodeRunner`` says an escaping exception is a bug in the node and the
    engine treats it as unretryable — recorded against the node rather than
    taking down the run's bookkeeping."""

    class _Boom(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            raise ValueError("kaboom")

    registry = _custom(_Boom())
    snapshot = _snapshot((GraphNode(key="n", node_type="test.node", version=1),), (), {})

    result = await invoke(snapshot, registry, "n", _context())

    assert isinstance(result, Failed)
    assert result.retryable is False
    assert "ValueError" in result.error
    assert "kaboom" in result.error


async def test_a_cancellation_is_not_swallowed() -> None:
    """The process being told to stop is not the node failing; swallowing it
    would leave the node RUNNING with nothing running it."""

    class _Cancelled(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            raise KeyboardInterrupt

    registry = _custom(_Cancelled())
    snapshot = _snapshot((GraphNode(key="n", node_type="test.node", version=1),), (), {})

    with pytest.raises(KeyboardInterrupt):
        await invoke(snapshot, registry, "n", _context())


async def test_the_real_manual_trigger_emits_the_run_payload() -> None:
    """The one-line M6 change, through the real registry."""

    snapshot = _snapshot(
        (GraphNode(key="t", node_type="trigger.manual", version=1),),
        (),
        {},
        trigger_payload={"order": 7},
    )
    registry = _registry()
    context = build_context(
        snapshot,
        registry,
        "t",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=1,
        attempt=1,
    )

    result = await invoke(snapshot, registry, "t", context)

    assert result == Completed(outputs={"main": {"order": 7}})


async def test_the_manual_trigger_emits_an_empty_object_when_started_with_nothing() -> None:
    snapshot = _snapshot((GraphNode(key="t", node_type="trigger.manual", version=1),), (), {})
    registry = _registry()
    context = build_context(
        snapshot,
        registry,
        "t",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=1,
        attempt=1,
    )

    result = await invoke(snapshot, registry, "t", context)

    assert result == Completed(outputs={"main": {}})


# --- Suspension (M7) --------------------------------------------------------


async def test_a_suspended_result_is_returned_untouched() -> None:
    """The engine reacts to the result type, never to the node type."""

    class _Waits(NodeRunner):
        async def run(self, context: NodeRunContext) -> NodeResult:
            return Suspended(resume_token="01ABCDEFGHJKMNPQRSTVWXYZ00", hint="why")

    registry = _custom(_Waits())
    snapshot = _snapshot((GraphNode(key="n", node_type="test.node", version=1),), (), {})

    result = await invoke(snapshot, registry, "n", _context())

    assert result == Suspended(resume_token="01ABCDEFGHJKMNPQRSTVWXYZ00", hint="why")


def test_a_fresh_invocation_carries_no_resume_token() -> None:
    snapshot = _snapshot((GraphNode(key="w", node_type="core.wait", version=1),), (), {})

    context = build_context(
        snapshot,
        _registry(),
        "w",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=1,
        attempt=1,
    )

    assert context.resume_token is None


def test_a_resumed_invocation_carries_the_token_that_resumed_it() -> None:
    snapshot = _snapshot((GraphNode(key="w", node_type="core.wait", version=1),), (), {})

    context = build_context(
        snapshot,
        _registry(),
        "w",
        run_id=1,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=1,
        attempt=1,
        resume_token="01ABCDEFGHJKMNPQRSTVWXYZ00",
    )

    assert context.resume_token == "01ABCDEFGHJKMNPQRSTVWXYZ00"


def test_the_idempotency_key_is_unchanged_by_a_resume() -> None:
    """Suspension is deliberate, not ambiguous, so the resumed invocation is the
    same logical attempt and can recognise the work it did before parking."""

    snapshot = _snapshot((GraphNode(key="w", node_type="core.wait", version=1),), (), {})

    first = build_context(
        snapshot,
        _registry(),
        "w",
        run_id=5,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=6,
        attempt=2,
    )
    resumed = build_context(
        snapshot,
        _registry(),
        "w",
        run_id=5,
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        workflow_node_id=6,
        attempt=2,
        resume_token="t",
    )

    assert first.idempotency_key == resumed.idempotency_key == "5:6:2"


# --- core.wait@1 ------------------------------------------------------------


async def _wait(inputs: dict[str, object], resume_token: str | None) -> NodeResult:
    registry = _registry()
    snapshot = _snapshot((GraphNode(key="w", node_type="core.wait", version=1),), (), {})
    context = NodeRunContext(
        config=core_wait.WaitConfig(),
        inputs=inputs,
        idempotency_key="1:1:1",
        organization_public_id="01ORGORGORGORGORGORGORGORG",
        trigger_payload={},
        resume_token=resume_token,
    )
    return await invoke(snapshot, registry, "w", context)


async def test_the_wait_node_suspends_on_its_first_invocation() -> None:
    result = await _wait({}, None)

    assert isinstance(result, Suspended)
    assert result.hint == "Waiting to be resumed."


async def test_the_wait_nodes_token_fits_the_storage_contract() -> None:
    """It borrows the project's identifier generator, so the engine can persist
    it without the node knowing anything about the column."""

    result = await _wait({}, None)

    assert isinstance(result, Suspended)
    assert len(result.resume_token) == PUBLIC_ID_LENGTH


async def test_each_suspension_mints_a_fresh_token() -> None:
    first = await _wait({}, None)
    second = await _wait({}, None)

    assert isinstance(first, Suspended)
    assert isinstance(second, Suspended)
    assert first.resume_token != second.resume_token


async def test_the_wait_node_completes_once_resumed() -> None:
    result = await _wait({}, "01ABCDEFGHJKMNPQRSTVWXYZ00")

    assert result == Completed()


async def test_a_resumed_wait_forwards_whatever_arrived() -> None:
    result = await _wait({"main": {"order": 7}}, "01ABCDEFGHJKMNPQRSTVWXYZ00")

    assert result == Completed(outputs={"main": {"order": 7}})


async def test_the_wait_node_is_pure_and_dispatched_like_any_other() -> None:
    """Suspending is the absence of a side effect, not one. ACTION rather than
    CONTROL: control nodes are the ones the engine interprets (Phase 7)."""

    descriptor = _registry().get("core.wait", 1)

    assert descriptor.side_effect is SideEffect.PURE
    assert descriptor.category is NodeCategory.ACTION
