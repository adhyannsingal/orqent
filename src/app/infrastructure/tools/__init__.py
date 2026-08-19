"""The tool catalogue, and the one place it is assembled.

``build_tool_registry`` is the single wiring point, mirroring
``build_registry`` for node types. Adding a tool is two edits: a module under
:mod:`.builtin`, and one line in ``_BUILT_INS``. No engine change, no schema
change, no migration, and no API change.

Explicit rather than discovered by scanning, for the reason the node catalogue is
(ADR-022): scanning makes a tool's presence depend on file layout and turns a
missing tool into a silent absence, where an explicit list makes it a diff.
"""

from __future__ import annotations

from typing import Final

from app.domain.tools.contract import Tool
from app.domain.tools.registry import ToolRegistry
from app.infrastructure.tools.builtin import calculator

_BUILT_INS: Final[tuple[Tool, ...]] = (calculator.TOOL,)


def build_tool_registry() -> ToolRegistry:
    """Assemble the catalogue.

    Pure: no database, no settings, no network, no credential. That is what lets
    a workflow be *validated* against it — see ``AgentConfig`` — without a
    deployment having to be configured for AI at all.
    """

    registry = ToolRegistry()
    for tool in _BUILT_INS:
        registry.register(tool)
    return registry


CATALOGUE: Final[ToolRegistry] = build_tool_registry()
"""The shipped catalogue, built once at import.

**A module-level constant, deliberately**, where the node registry is built per
container. The difference is that a node's runner needs injected dependencies —
an ``AgentRunner``, a knowledge retriever — so which nodes exist is a property of
the *deployment*. A tool needs nothing injected, so which tools exist is a
property of the *release*.

That distinction is what makes authoring-time validation honest: a workflow
naming a tool is accepted or rejected identically in every environment, and a
version that published cannot fail at run time because some other process was
wired differently.
"""
