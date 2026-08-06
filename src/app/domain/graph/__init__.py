"""The workflow graph and the vocabulary for what is wrong with one.

Pure data and pure functions: no session, no HTTP, no registry lookups at
construction. That is what makes the hardest logic in Phase 4 — validation —
exhaustively testable from fixtures rather than from a database.

Named ``graph`` rather than ``workflow`` because "workflow" is already taken
twice over: there is a ``Workflow`` row and a ``WorkflowService``. This package
holds the shape those things persist and operate on, and nothing else.
"""
