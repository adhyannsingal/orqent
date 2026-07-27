"""Security adapters — concrete implementations of the domain's security ports.

This package is the *only* place in the application permitted to import
``argon2`` or ``jwt``. Everything crossing its boundary is either a plain
``str`` or a domain value object, so the hashing algorithm and the token format
can both be replaced without any other layer noticing.
"""
