"""The node contract — what a node *is*, independent of what any node does.

Everything the engine will ever need to know about a node lives here: its typed
handles, its descriptor, the result its runner may return, and the two ports
(``NodeRunner``, ``NodeRegistry``) through which concrete nodes are reached.

Nothing in this package knows that HTTP, email, or language models exist. That
is the point: an AI agent node and an email node reach the engine through
exactly the same contract, so adding a node type requires no engine change
(ADR-020).

Pure Python plus Pydantic, which ADR-031 permits here for config models and for
naming the shape of a ``Record`` handle. No FastAPI, no SQLAlchemy, no driver —
asserted by a test, not by convention.
"""
