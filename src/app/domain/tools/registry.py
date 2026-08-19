"""The catalogue of tools an agent may be given (Phase 10, M6).

Deliberately the same shape as the node registry (ADR-022): an in-process map
from a stable name to a trusted implementation, populated once from an explicit
list. No table, no plugin loader, no user-supplied code, and no ``if name ==``
anywhere else in the tree.

Concrete rather than a port, unlike ``NodeRegistry``. That port exists because
the *engine* resolves runners through it and must not know the implementation;
nothing here has a second implementation or an inward consumer needing to be
insulated from one. A test builds a real registry containing fake tools, which is
simpler than a fake registry and exercises the same lookup and refusal paths.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.domain.nodes.descriptor import SideEffect
from app.domain.tools.contract import Tool, ToolDefinition, ToolError


class DuplicateToolError(Exception):
    """Two tools claimed the same name.

    Not an :class:`~app.domain.errors.AppError`: this can only happen while
    assembling a registry from source, so it is a programming error that must
    stop the process rather than a condition a request can produce. Failing at
    startup is the point — the alternative is one tool silently shadowing
    another, which for a capability a model can invoke is a security bug, not an
    inconvenience.
    """


class UnknownToolError(ToolError):
    """A tool was asked for by a name this deployment does not ship."""

    def __init__(self, name: str) -> None:
        # The requested name is echoed, and nothing else. It came from a model
        # or from stored configuration; either way it is short, non-secret, and
        # the only detail that makes the failure actionable.
        super().__init__(f"Unknown tool: {name!r}.", retryable=False)


class ToolRegistry:
    """Stable names to trusted implementations."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Add a tool.

        **Refuses anything that is not ``PURE``**, and refuses it here rather
        than at execution. M6 restricts itself to tools that are free to repeat
        (ADR-024: execution is at-least-once, so a recovered agent may request
        the same tool again), and the honest way to hold a restriction is to
        make the unsafe thing unrepresentable in a running system — not to
        document it and hope. A side-effecting tool needs idempotency threaded
        to the external system, and that machinery is a later milestone.
        """

        definition = tool.definition
        if definition.name in self._tools:
            raise DuplicateToolError(
                f"{definition.name!r} is already registered; tool names are "
                "append-only and are referenced by published workflows."
            )
        if definition.side_effect is not SideEffect.PURE:
            raise DuplicateToolError(
                f"{definition.name!r} declares {definition.side_effect}, but M6 "
                "executes only PURE tools: an agent may be retried, and a "
                "repeated tool call must be free."
            )
        self._tools[definition.name] = tool

    def get(self, name: str) -> Tool:
        """The tool by that name. Raises :class:`UnknownToolError`."""

        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(name)
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> Sequence[str]:
        """Every registered name, in registration order.

        Insertion-ordered rather than sorted, for the same reason the node
        catalogue is: registration is an explicit list, so the order is already
        deterministic and is the order a human chose.
        """

        return tuple(self._tools)

    def definitions(self, names: Iterable[str]) -> Sequence[ToolDefinition]:
        """Definitions for ``names``, in the order given.

        The order is the caller's because it is the author's: a workflow lists
        its tools, and the model is shown them in that order. Raises
        :class:`UnknownToolError` for a name this registry does not have, rather
        than skipping it — silently showing a model fewer tools than the
        workflow asked for would make a misconfiguration look like the model
        simply choosing not to use one.
        """

        return tuple(self.get(name).definition for name in names)
