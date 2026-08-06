"""Node catalogue endpoint.

Publishes what this deployment can run. The visual builder depends on nothing
else to draw its palette and configuration forms, which is why this endpoint
lands early: once it exists, frontend and backend work proceed independently.

Read-only and derived entirely from code (ADR-022), so there is no database
access here and no service layer between the route and the registry — there
would be nothing for one to decide.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import NodeRegistryDep
from app.api.security import CurrentUserDep
from app.domain.nodes.descriptor import NodeDescriptor
from app.schemas.node_types import (
    InputHandleResponse,
    NodeCatalogResponse,
    NodeDisplayResponse,
    NodeTypeResponse,
    OutputHandleResponse,
)

router = APIRouter(tags=["node-types"])


def _to_response(descriptor: NodeDescriptor) -> NodeTypeResponse:
    """Project a descriptor onto its wire representation.

    Lives here rather than on the schema so ``app.schemas`` stays free of domain
    imports. This is the boundary: descriptors do not travel outward, and the
    ``config_model`` class itself never appears in the payload — only the JSON
    Schema generated from it.
    """

    return NodeTypeResponse(
        # The wire says `type`; the descriptor field is `node_type` because the
        # former shadows a builtin inside that class body.
        type=descriptor.node_type,
        version=descriptor.version,
        qualified_name=descriptor.qualified_name,
        category=descriptor.category.value,
        deprecated=descriptor.deprecated,
        display=NodeDisplayResponse(
            label=descriptor.display.label,
            description=descriptor.display.description,
            icon=descriptor.display.icon,
            color=descriptor.display.color,
        ),
        config_schema=descriptor.config_model.model_json_schema(),
        inputs=[
            InputHandleResponse(
                name=handle.name,
                # str(HandleType) renders `List<Text>` recursively, and is the
                # same text validation messages quote.
                type=str(handle.type),
                arity=handle.arity.value,
                join=handle.join.value,
                required=handle.required,
            )
            for handle in descriptor.inputs
        ],
        outputs=[
            OutputHandleResponse(name=handle.name, type=str(handle.type))
            for handle in descriptor.outputs
        ],
    )


@router.get(
    "",
    response_model=NodeCatalogResponse,
    summary="List every node type this deployment can run",
)
async def list_node_types(
    current_user: CurrentUserDep,
    registry: NodeRegistryDep,
) -> NodeCatalogResponse:
    # Authentication only, no role check: the catalogue is the same for everyone
    # in the organization and reveals nothing about their data.
    #
    # Order comes from the registry, which preserves registration order, so
    # repeated calls are byte-identical and frontend snapshots do not churn.
    return NodeCatalogResponse(items=[_to_response(d) for d in registry.all()])
