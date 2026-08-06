"""Built-in node types.

One module per node type, each holding everything that node is: its
configuration model, its descriptor, and its runner. Keeping the three together
is what makes the claim in ADR-020 true — adding a node type means adding a
module here and one line to the registry's list, and touching nothing else.

Every config model sets ``extra="forbid"``. A key the model does not declare is
almost always a typo or a stale value left by an older builder, and silently
accepting it would let a workflow publish with configuration that does nothing.
The registry conformance test asserts this for every registered type, so the
policy holds without a shared base class the modules would have to inherit from.
"""
