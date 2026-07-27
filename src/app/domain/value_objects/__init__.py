"""Value objects — immutable types defined entirely by their values.

Unlike entities, these have no identity and no lifecycle: two instances with
equal fields are interchangeable. All are frozen dataclasses of pure Python, so
they can be passed freely across layers without dragging in a framework.
"""
