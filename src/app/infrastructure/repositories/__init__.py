"""Repositories — persistence access, one per aggregate.

Each repository wraps a single ``AsyncSession`` and translates intent ("find the
live user with this email") into SQL. They are reached through
:class:`SqlAlchemyUnitOfWork`, which owns the session and the transaction, so
every repository in one unit of work reads and writes inside that transaction.

Deliberately **not** ports. Per ADR-008 the ORM models *are* the data model —
there is no separate domain entity or mapping layer — so a repository returns
``User``, ``Organization``, and friends directly. An abstract port in
``app.domain`` would have to name those SQLAlchemy types in its signatures,
inverting the very dependency it exists to protect. Services may depend on
infrastructure; the domain may not.

Repositories hold **no business logic**: no password or token hashing, no policy
about who may log in, no error raising beyond what the database itself enforces.
They answer questions and record rows. Deciding what an answer *means* is the
service layer's job.

One shared rule: **soft-deleted rows are treated as absent.** A user with
``deleted_at`` set is returned by no lookup here, so callers never have to
remember to filter.
"""
