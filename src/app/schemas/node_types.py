"""Node catalogue response models.

Transport only. This payload is the entire contract between the backend and the
visual builder: the palette, the config forms, and the connection dots are all
rendered from it, so adding a node type must require no frontend release.

Handle types travel as strings — ``"Text"``, ``"List<Text>"`` — the same
rendering used in validation messages, so a user reading "cannot connect
List<Text> to Text" sees the words the builder showed them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class NodeDisplayResponse(BaseModel):
    """How the builder should render a node type."""

    label: str
    description: str
    icon: str | None = None
    color: str | None = None


class InputHandleResponse(BaseModel):
    """A socket edges may arrive at."""

    name: str
    type: str
    arity: str
    """``single`` or ``many`` — whether more than one edge may arrive."""

    join: str
    """``all`` or ``any``. Declared now, meaningful once branching exists."""

    required: bool
    """Whether publishing demands at least one inbound edge."""


class OutputHandleResponse(BaseModel):
    """A socket edges may leave from."""

    name: str
    type: str


class NodeTypeResponse(BaseModel):
    """One entry in the palette."""

    type: str
    version: int
    qualified_name: str
    """``core.constant@1`` — how a node instance pins this type."""

    category: str
    deprecated: bool
    """Flagged, never hidden: a deprecated type must stay visible so a workflow
    already using it remains explicable."""

    display: NodeDisplayResponse
    config_schema: dict[str, Any]
    """JSON Schema generated from the node's Pydantic config model. The builder
    renders the configuration form from this and nothing else."""

    inputs: list[InputHandleResponse]
    outputs: list[OutputHandleResponse]


class NodeCatalogResponse(BaseModel):
    """Every node type this deployment can run.

    Unpaginated on purpose: the catalogue is small, fixed at build time, and the
    builder needs all of it to draw a palette. Order is the server's, and stable.
    """

    items: list[NodeTypeResponse]
