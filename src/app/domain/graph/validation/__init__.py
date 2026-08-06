"""Graph validation — the rules a workflow must satisfy to be publishable.

Each module here answers one question about a graph and returns
:class:`~app.domain.graph.issues.ValidationIssue` values; none of them raises,
because a builder needs every problem at once rather than the first one.

Pure functions over data: a graph and, where the rule depends on what a node
*is*, the descriptors that were already resolved for it. No registry, no
session, no HTTP. That is what makes the hardest logic in Phase 4 testable from
fixtures alone.

The pipeline that runs these in order and assembles a report is added in M8.
"""
