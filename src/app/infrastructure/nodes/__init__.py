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

from typing import Final

from app.domain.nodes.descriptor import NodeDescriptor
from app.domain.nodes.registry import NodeRegistry
from app.domain.nodes.runner import NodeRunner
from app.infrastructure.nodes.builtin import (
    core_constant,
    core_log,
    core_noop,
    core_wait,
    trigger_manual,
)
from app.infrastructure.nodes.registry import InMemoryNodeRegistry

# Order is the order the builder's palette shows: triggers first, then the
# things you connect to them.
_BUILT_INS: Final[tuple[tuple[NodeDescriptor, NodeRunner], ...]] = (
    (trigger_manual.DESCRIPTOR, trigger_manual.RUNNER),
    (core_constant.DESCRIPTOR, core_constant.RUNNER),
    (core_noop.DESCRIPTOR, core_noop.RUNNER),
    (core_log.DESCRIPTOR, core_log.RUNNER),
    (core_wait.DESCRIPTOR, core_wait.RUNNER),
)


def build_registry() -> NodeRegistry:
    """Assemble the catalogue.

    Pure: no database, no settings, no I/O. That is what lets the container
    build it eagerly and every test build an identical one in a line.
    """

    registry = InMemoryNodeRegistry()
    for descriptor, runner in _BUILT_INS:
        registry.register(descriptor, runner)
    return registry
