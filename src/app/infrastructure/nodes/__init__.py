"""The node catalogue and the one place it is assembled.

``build_registry`` is the single wiring point for built-in node types. Adding a
node type is two edits: a new module under :mod:`.builtin`, and one line in
``_BUILT_INS`` below. No engine change, no schema change, no API change — the
property ADR-020 exists to guarantee.

Registration is explicit rather than discovered by scanning the package. Scanning
would make a node's presence depend on file layout and turn a missing node into
a silent absence; an explicit list makes it a visible diff, and makes the order
of the catalogue a decision rather than an accident of the filesystem.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from app.domain.nodes.descriptor import NodeDescriptor
from app.domain.nodes.registry import NodeRegistry
from app.domain.nodes.runner import NodeRunner
from app.domain.ports.agent_runner import AgentRunner
from app.domain.ports.knowledge import KnowledgeRetriever
from app.infrastructure.llm.mock_agent_runner import MockAgentRunner
from app.infrastructure.nodes.builtin import (
    ai_agent,
    core_condition,
    core_constant,
    core_log,
    core_merge,
    core_noop,
    core_wait,
    trigger_manual,
    trigger_schedule,
    trigger_webhook,
)
from app.infrastructure.nodes.registry import InMemoryNodeRegistry

# Order is the order the builder's palette shows: triggers first, then the
# things you connect to them.
_BUILT_INS: Final[tuple[tuple[NodeDescriptor, NodeRunner], ...]] = (
    (trigger_manual.DESCRIPTOR, trigger_manual.RUNNER),
    (trigger_webhook.DESCRIPTOR, trigger_webhook.RUNNER),
    (trigger_schedule.DESCRIPTOR, trigger_schedule.RUNNER),
    (core_constant.DESCRIPTOR, core_constant.RUNNER),
    (core_noop.DESCRIPTOR, core_noop.RUNNER),
    (core_log.DESCRIPTOR, core_log.RUNNER),
    (core_wait.DESCRIPTOR, core_wait.RUNNER),
    (core_condition.DESCRIPTOR, core_condition.RUNNER),
    (core_merge.DESCRIPTOR, core_merge.RUNNER),
)


def build_registry(
    agents: AgentRunner | None = None,
    knowledge: Callable[[], KnowledgeRetriever] | None = None,
) -> NodeRegistry:
    """Assemble the catalogue.

    Pure: no database, no settings, no I/O. That is what lets the container
    build it eagerly and every test build an identical one in a line.

    ``agents`` is the one dependency any built-in has, and it belongs to
    ``ai.agent@1`` alone — the port through which that node reaches a model
    without importing one (ADR-013). It is optional because the seventy-odd
    existing callers want a catalogue for *authoring*, validation, or the
    node-type API, and never invoke a runner at all; requiring it would have
    meant touching every one of them to say something they do not care about.

    ``knowledge`` is the second, and belongs to the same node: how it reaches the
    organization's documents (M5). A **factory**, because constructing a
    retriever needs an embedding credential this deployment may not have, and a
    catalogue must be buildable without one — see ``AgentNodeRunner``. Omitting
    it yields a catalogue that cannot retrieve, which is correct for authoring
    and validation and fails loudly rather than silently if an agent configured
    for retrieval is ever executed through it.

    **The default is a deterministic mock, and the container passes one
    explicitly anyway.** Defaulting keeps tests and tooling to a single line;
    passing explicitly at the composition root means that when M2 adds a real
    adapter, which one is in use is a visible decision in one file rather than a
    fallback nobody notices is still in effect.
    """

    registry = InMemoryNodeRegistry()
    for descriptor, runner in _BUILT_INS:
        registry.register(descriptor, runner)
    # Registered here rather than in `_BUILT_INS` because it is the only node
    # whose runner is constructed rather than a module-level singleton.
    registry.register(ai_agent.DESCRIPTOR, ai_agent.runner(agents or MockAgentRunner(), knowledge))
    return registry
