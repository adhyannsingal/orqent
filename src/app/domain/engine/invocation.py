"""Running one node — everything between "start this" and a ``NodeResult``.

The scheduler decides *which* node runs (``scheduler.py``); this decides *what
happens when it does*. Both are domain: the only outward thing here is the
:class:`~app.domain.nodes.registry.NodeRegistry` **port**, through which a node
type name becomes a runner. The engine never imports a concrete node, never
constructs the registry, and never learns what any node type does — which is the
mechanical form of ADR-014 and ADR-020.

Three steps, and the middle one is the whole point of the node contract:

1. **Resolve inputs** by walking the persisted edges. Handle to handle, no
   expression language, no evaluator, no coercion (ADR-021 already proved at
   authoring time that the value fits).
2. **Build the context** every runner receives — identical for an HTTP call, an
   email, and an AI agent.
3. **Invoke**, and turn an escaped exception into a failure, because the engine
   must record a broken node against that node rather than lose the run.

Phase 6 M6 scope: ``Completed`` and ``Failed``. ``Suspended`` is a legal
``NodeResult`` and is returned untouched — deciding what a suspended node *means*
for the run is M7, and pretending here that it cannot happen would be the kind of
partial handling that hides a bug.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.engine.snapshot import RunSnapshot
from app.domain.errors import DomainRuleError
from app.domain.nodes.registry import NodeRegistry
from app.domain.nodes.result import Failed, NodeResult
from app.domain.nodes.runner import NodeRunContext


def resolve_inputs(snapshot: RunSnapshot, node_key: str) -> Mapping[str, object]:
    """The values arriving on this node's input handles.

    For every inbound edge, the upstream execution's ``outputs[source_handle]``
    becomes ``inputs[target_handle]``. That is the entire data-flow mechanism:
    the graph says what connects to what, and the values move (ADR-023).

    **An upstream handle that produced nothing leaves the input absent**, not
    ``None`` — ``Completed.outputs`` documents that a missing handle is how a
    conditional output stays silent, and ``NodeRunContext.inputs`` documents that
    "not connected" and "connected to null" must stay distinguishable. Collapsing
    the two would make a node unable to tell them apart.

    An upstream that has not run yet is likewise absent rather than an error: the
    scheduler only starts a node whose upstreams have all succeeded, so this
    cannot happen for a node about to run, and guarding it here would suggest
    otherwise.
    """

    inputs: dict[str, object] = {}
    for edge in snapshot.graph.incoming(node_key):
        upstream = snapshot.node_executions.get(edge.source_key)
        if upstream is None or upstream.outputs is None:
            continue
        if edge.source_handle in upstream.outputs:
            # Moved, not converted. The type lattice settled compatibility when
            # the edge was drawn (ADR-021); coercing now would be a second,
            # quieter type system.
            inputs[edge.target_handle] = upstream.outputs[edge.source_handle]
    return inputs


def idempotency_key(run_id: int, workflow_node_id: int, attempt: int) -> str:
    """The key a runner deduplicates against (ADR-024).

    Stable for the whole of one attempt and different for the next, so a node
    that reached an external system before its process died can recognise its own
    earlier call. Execution is at-least-once; this is what lets a node author do
    something about that.

    Derived from ``(run_id, workflow_node_id, attempt)`` rather than ADR-024's
    full ``(run_id, node_id, scope_path, iteration, attempt)``: Phase 6 has no
    scopes and no iteration, so those components are constants and the key is
    exactly as unique. **Never persisted** — Phase 7 changes its shape when loops
    arrive, and a stored key would become a contract to migrate.
    """

    return f"{run_id}:{workflow_node_id}:{attempt}"


def build_context(
    snapshot: RunSnapshot,
    registry: NodeRegistry,
    node_key: str,
    *,
    run_id: int,
    workflow_node_id: int,
    attempt: int,
) -> NodeRunContext:
    """Assemble what the runner is handed.

    The configuration is instantiated from the node's stored JSON against the
    descriptor's ``config_model``, because a runner is promised a validated model
    and never raw JSON. Publishing already validated it, so a failure here means
    the persisted graph and the registry have diverged — an append-only violation
    (ADR-022) or a deployment older than its data — and it is reported as such
    rather than passed to a node that cannot use it.
    """

    node = snapshot.graph.node(node_key)
    if node is None:
        raise DomainRuleError(f"The graph has no node {node_key!r}.")

    descriptor = registry.get(node.node_type, node.version)
    try:
        config = descriptor.config_model(**node.config)
    except Exception as error:  # A broken config must name the node, not crash the run.
        raise DomainRuleError(
            f"Node {node_key!r} has configuration its type {descriptor.qualified_name} "
            f"no longer accepts: {error}"
        ) from error

    return NodeRunContext(
        config=config,
        inputs=resolve_inputs(snapshot, node_key),
        idempotency_key=idempotency_key(run_id, workflow_node_id, attempt),
        # Every node is handed the payload; only a trigger reads it. That is what
        # lets data enter a graph whose first node has no inbound edge, without
        # the engine learning what a trigger is (ADR-014).
        trigger_payload=snapshot.trigger_payload or {},
    )


async def invoke(
    snapshot: RunSnapshot, registry: NodeRegistry, node_key: str, context: NodeRunContext
) -> NodeResult:
    """Run the node and report what happened.

    Never raises for a node's own misbehaviour. ``NodeRunner`` states that an
    escaping exception is a bug in the node and that the engine treats it as an
    unretryable failure — so it is caught here and recorded against the node,
    which keeps one broken node from taking down the run's bookkeeping with it.

    ``BaseException`` is deliberately *not* caught: a cancellation or a
    ``KeyboardInterrupt`` is the process being told to stop, not the node
    failing, and swallowing it would leave the node ``RUNNING`` with nothing
    running it. Recovery already handles that case correctly.
    """

    node = snapshot.graph.node(node_key)
    if node is None:
        raise DomainRuleError(f"The graph has no node {node_key!r}.")

    runner = registry.runner(node.node_type, node.version)
    try:
        return await runner.run(context)
    except Exception as error:  # A node's bug is recorded, not propagated.
        return Failed(error=f"{type(error).__name__}: {error}", retryable=False)
