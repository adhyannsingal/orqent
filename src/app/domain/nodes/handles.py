"""Typed handles — the sockets an edge connects.

A handle is a named, typed socket on a node. Edges join one output handle to one
input handle, and the type system here is what lets the authoring layer refuse
an impossible connection *before* anything runs (ADR-021).

The vocabulary is deliberately closed. Arbitrary JSON Schema subsumption is
close to undecidable in the general case and produces errors no visual-builder
user can act on; eight kinds, two of them parameterised, can always answer
"these do not connect, and here is why" in one sentence.

Called *handles* rather than *ports* on purpose: this codebase already uses
"port" for hexagonal ports (``UnitOfWork``, ``NodeRunner``), and the visual
builder will use the React Flow term.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel

# Bounded by workflow_edges.source_handle / target_handle, which are VARCHAR(64).
# Declared here so the contract states its own limit rather than deferring to a
# schema the domain cannot see.
MAX_HANDLE_NAME_LENGTH: Final = 64


class HandleKind(StrEnum):
    """The eight members of the closed type lattice (ADR-021)."""

    ANY = "Any"
    TEXT = "Text"
    NUMBER = "Number"
    BOOLEAN = "Boolean"
    JSON = "Json"
    RECORD = "Record"
    BINARY = "Binary"
    LIST = "List"


@dataclass(frozen=True, slots=True)
class HandleType:
    """A handle's type: a kind, plus a parameter for the two kinds that take one.

    ``List`` carries the type of its items and ``Record`` carries the model that
    describes its shape; every other kind carries neither. Parameterisation is
    not decoration — compatibility for lists recurses into the item type, and
    two records are compatible only when they name the same model, so a flat
    enum could not express either rule.

    Construct through the module constants and factories (``TEXT``,
    ``list_of``, ``record``) rather than directly; they are what make the
    invariants below unreachable by accident.
    """

    kind: HandleKind
    item: HandleType | None = None
    """Item type — required for ``List``, forbidden otherwise."""

    model: type[BaseModel] | None = None
    """Shape — required for ``Record``, forbidden otherwise."""

    def __post_init__(self) -> None:
        # A List with no item type, or a Text that somehow carries one, would
        # make the compatibility rules ambiguous rather than merely wrong.
        if (self.kind is HandleKind.LIST) != (self.item is not None):
            raise ValueError("List requires an item type, and only List may have one.")
        if (self.kind is HandleKind.RECORD) != (self.model is not None):
            raise ValueError("Record requires a model, and only Record may have one.")

    def __str__(self) -> str:
        """Render as it appears in validation messages: ``List<Record<Invoice>>``."""

        if self.kind is HandleKind.LIST:
            return f"List<{self.item}>"
        if self.kind is HandleKind.RECORD:
            # `model` is never None for RECORD — guaranteed by __post_init__.
            assert self.model is not None
            return f"Record<{self.model.__name__}>"
        return self.kind.value


ANY: Final = HandleType(HandleKind.ANY)
TEXT: Final = HandleType(HandleKind.TEXT)
NUMBER: Final = HandleType(HandleKind.NUMBER)
BOOLEAN: Final = HandleType(HandleKind.BOOLEAN)
JSON: Final = HandleType(HandleKind.JSON)
BINARY: Final = HandleType(HandleKind.BINARY)


def list_of(item: HandleType) -> HandleType:
    """A list whose elements are ``item``."""

    return HandleType(HandleKind.LIST, item=item)


def record(model: type[BaseModel]) -> HandleType:
    """A record shaped like ``model``."""

    return HandleType(HandleKind.RECORD, model=model)


class Arity(StrEnum):
    """How many edges may arrive at one input handle."""

    SINGLE = "single"
    MANY = "many"


class Join(StrEnum):
    """How a handle receiving several edges decides it is satisfied.

    Declared from Phase 4 so the contract is complete, but only meaningful once
    branching exists (Phase 6): ``ALL`` waits for every inbound edge, ``ANY``
    proceeds on the first. Phase 4 enforces :class:`Arity` alone.
    """

    ALL = "all"
    ANY = "any"


def _validate_handle_name(name: str) -> None:
    """Reject names the schema or the wire format could not carry."""

    if not name or not name.strip():
        raise ValueError("Handle name must not be blank.")
    if len(name) > MAX_HANDLE_NAME_LENGTH:
        raise ValueError(f"Handle name exceeds {MAX_HANDLE_NAME_LENGTH} characters: {name!r}")


@dataclass(frozen=True, slots=True)
class InputHandle:
    """A socket edges arrive at."""

    name: str
    type: HandleType
    arity: Arity = Arity.SINGLE
    join: Join = Join.ALL
    required: bool = True
    """Whether validation demands at least one inbound edge."""

    def __post_init__(self) -> None:
        _validate_handle_name(self.name)


@dataclass(frozen=True, slots=True)
class OutputHandle:
    """A socket edges leave from.

    Carries no arity: an output may feed any number of downstream handles, and
    that is structural parallelism rather than a property to configure.
    """

    name: str
    type: HandleType

    def __post_init__(self) -> None:
        _validate_handle_name(self.name)
