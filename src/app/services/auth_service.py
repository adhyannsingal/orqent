"""Authentication use cases.

One method per use case, each owning exactly one transaction (ADR-009). The
service orchestrates: it decides *what* must happen and in what order, and
delegates *how* to the ports and repositories.

What it deliberately does not contain: any SQL (persistence goes through the
repositories on the unit of work), any knowledge that passwords are Argon2 or
that tokens are JWTs (both sit behind domain ports), and any HTTP concept. It
raises domain errors, which the API layer alone maps onto status codes.

It does import SQLAlchemy's ``IntegrityError`` and the ORM models. That is not
the leak it might look like: ADR-008 makes the ORM models the data model, so
SQLAlchemy is a legitimate service-layer dependency. The strict containment
applies to the things hidden behind ports — a ``jwt`` or ``argon2`` import here
would be a real violation, and there is none.

Concurrency, for :meth:`AuthService.refresh`
--------------------------------------------
Rotation makes a refresh token single-use, so two requests arriving with the
same token must not both succeed. ``SELECT ... FOR UPDATE`` on the token's row
is what guarantees that, for two reasons that both matter:

* It serialises the two requests. The row is found through a unique index, so
  InnoDB takes a single record lock; the second transaction blocks on it until
  the first commits, and no gap lock is involved to widen contention.
* It reads the *current* row, not a snapshot. MySQL defaults to REPEATABLE
  READ, under which a plain ``SELECT`` would serve the second transaction the
  version of the row as of *its own* first read — still showing ``revoked_at``
  as NULL even after the first request revoked it. A locking read is a "current
  read" and always sees the latest committed state, so the second request
  observes the revocation. A plain SELECT here would silently permit double
  rotation; the lock is load-bearing, not defensive.

The revocation and the successor insert then happen in that same transaction,
so there is no instant at which the presented token is dead and no replacement
exists — a crash between them rolls back both.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import structlog
from sqlalchemy.exc import IntegrityError

from app.domain.errors import AuthenticationError, ConflictError, InfrastructureError
from app.domain.ports.password_hasher import PasswordHasher
from app.domain.ports.password_reset_notifier import (
    PasswordResetDeliveryError,
    PasswordResetNotifier,
)
from app.domain.ports.token_service import TokenService
from app.domain.value_objects.authenticated_user import AuthenticatedUser
from app.domain.value_objects.token import TokenClaims, TokenType
from app.domain.value_objects.token_pair import TokenPair
from app.infrastructure.db.identifiers import new_public_id
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.password_reset_token import PasswordResetToken
from app.infrastructure.db.models.refresh_token import RefreshToken
from app.infrastructure.db.models.user import User
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.security.password_reset_token import new_password_reset_token
from app.infrastructure.security.token_hashing import hash_token, verify_token_hash

log = structlog.get_logger(__name__)

# The role every registrant receives: they are the sole member of a brand-new
# organization, so they own it (ADR-011). Seeded by migration, never created at
# runtime.
DEFAULT_ROLE = "owner"

# One message for every credential failure — unknown email, wrong password, and
# disabled account are indistinguishable to the caller. Telling them apart would
# turn the login form into an account-enumeration oracle.
_INVALID_CREDENTIALS = "Either email or password is incorrect."

# The same idea for the refresh endpoint: unknown, expired, revoked, replayed,
# and belonging-to-a-disabled-account all read identically from outside. A
# client cannot act on the difference, and an attacker should not learn it —
# in particular, "this token was replayed" must not be observable, or probing
# would reveal which stolen tokens are still live.
_INVALID_REFRESH = "Invalid or expired refresh token."

# The one answer `forgot_password` ever gives. It is phrased conditionally
# because the server genuinely will not say which case it is in: an address with
# an account and one without must be indistinguishable, or the endpoint becomes
# a bulk membership oracle that needs no password guessing at all.
_RESET_REQUESTED = "If an account exists for that email, a password reset link has been sent."

# The same idea for redeeming a link: unknown, expired, already used, and
# superseded by a newer request all read identically. A client cannot act on the
# difference, and telling them apart would confirm to an attacker holding a
# stale token that it was once real and whose it was.
_INVALID_RESET = "Password reset link is invalid or expired."

# A real Argon2id hash of a random throwaway string, used only to spend the same
# CPU time verifying a password for an email that does not exist as for one that
# does. It is not a secret and unlocks nothing: no account has this hash, and
# the password behind it was discarded when it was generated.
#
# It must carry the *current* cost parameters, or a failed lookup would be
# measurably cheaper than a real verification and reopen the timing channel.
# ``test_dummy_hash_uses_current_parameters`` fails if the defaults move.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$XYNFZrkq4k5kNOvGDj1Pdw"
    "$3DSEcCN8SO4Fc5mcGa9MvBO/T4cS/gsRUS5U98puT2M"
)

# How many `name-2`, `name-3`, ... candidates to try before falling back to a
# guaranteed-unique suffix. Bounded because each attempt costs a query.
_MAX_SLUG_ATTEMPTS = 5

# Leaves room inside organizations.slug (VARCHAR(255)) for a suffix.
_MAX_SLUG_BASE_LENGTH = 200


def _as_utc(value: datetime) -> datetime:
    """Read a stored timestamp as the UTC instant it represents.

    MySQL ``DATETIME`` carries no timezone, so a value written as aware UTC
    comes back naive. Comparing that against an aware ``now`` raises TypeError,
    which would make every real refresh fail while in-memory tests — where the
    value never round-trips through the driver — kept passing. Values that are
    already aware are returned untouched.
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _slugify(value: str) -> str:
    """Reduce a display name to a URL-safe slug.

    Accents are folded to ASCII first (``Café`` → ``cafe``) rather than dropped,
    so a non-English name yields something recognisable instead of a fragment.
    """

    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    # A name of nothing but punctuation or non-Latin script can slugify to "";
    # fall back so the suffix logic always has something to build on.
    return slug[:_MAX_SLUG_BASE_LENGTH] or "org"


class AuthService:
    """Registration and login."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
        password_hasher: PasswordHasher,
        token_service: TokenService,
        password_reset_notifier: PasswordResetNotifier,
        *,
        password_reset_ttl_seconds: int,
        password_reset_url_base: str | None,
    ) -> None:
        """Take a *factory* for units of work, not a unit of work.

        Each use case then opens its own transaction, so "one transaction per
        use case" holds structurally rather than depending on how long this
        service happens to live. A shared instance would also be unsafe under
        concurrency: two requests would interleave writes on one session.
        """

        self._unit_of_work_factory = unit_of_work_factory
        self._password_hasher = password_hasher
        self._token_service = token_service
        self._password_reset_notifier = password_reset_notifier
        self._password_reset_ttl_seconds = password_reset_ttl_seconds
        self._password_reset_url_base = password_reset_url_base

    async def register(
        self,
        *,
        email: str,
        password: str,
        organization_name: str,
    ) -> User:
        """Create an organization, its first user, and grant them ownership.

        All three land in one transaction: a user without an organization, or an
        organization with no owner, would be corrupt state.

        Raises :class:`ConflictError` if the email already belongs to a live
        account, and :class:`InfrastructureError` if the role catalog has not
        been seeded.
        """

        normalized_email = self._normalize_email(email)

        try:
            async with self._unit_of_work_factory() as uow:
                # Checked up front so the ordinary duplicate-signup case gets a
                # precise message. The unique index is still the guarantee — see
                # the IntegrityError handler for the race this leaves open.
                if await uow.users.get_by_email(normalized_email) is not None:
                    raise ConflictError("An account with this email address already exists.")

                role = await uow.roles.get_by_name(DEFAULT_ROLE)
                if role is None:
                    # A deployment problem, not a client one: the catalog is
                    # seeded by migration. 503 says "this server is not ready",
                    # which is exactly true and becomes false once seeded.
                    raise InfrastructureError(
                        f"The {DEFAULT_ROLE!r} role is missing; "
                        "the role catalog has not been seeded."
                    )

                organization = await uow.organizations.add(
                    Organization(
                        name=organization_name,
                        slug=await self._available_slug(uow, organization_name),
                    )
                )

                user = await uow.users.add(
                    User(
                        email=normalized_email,
                        password_hash=await self._hash_password(password),
                        # Assigned through the relationship, not the raw FK, so
                        # the returned User already has `organization` populated
                        # — under asyncio a later lazy load would raise.
                        organization=organization,
                    )
                )
                await uow.roles.assign_to_user(user.id, role.id)

                # Re-read through the repository so the returned object carries
                # its eagerly-loaded roles too, making it safe to serialize
                # outside the session.
                registered = await uow.users.get_by_id(user.id)
                await uow.commit()
        except IntegrityError as exc:
            # Reached when a concurrent registration claimed the email or slug
            # between the checks above and this commit. The unit of work has
            # already rolled back; all that remains is to say so in domain terms.
            raise ConflictError("An account with this email address already exists.") from exc

        # get_by_id cannot miss a row this transaction just wrote, but the
        # repository's signature is honest about returning None.
        if registered is None:  # pragma: no cover - unreachable
            raise InfrastructureError("The newly registered user could not be read back.")

        log.info("user_registered", user=registered.public_id, organization=organization.public_id)
        return registered

    async def login(self, *, email: str, password: str) -> TokenPair:
        """Verify credentials and issue a fresh token pair.

        Raises :class:`AuthenticationError`, with one identical message, for
        every way this can fail.
        """

        normalized_email = self._normalize_email(email)

        async with self._unit_of_work_factory() as uow:
            user = await uow.users.get_by_email(normalized_email)

            if user is None:
                # Spend the same CPU as a real verification before failing.
                # Returning early here is what makes "no such account" and
                # "wrong password" tell apart by a stopwatch.
                await self._verify_password(password, _DUMMY_PASSWORD_HASH)
                raise AuthenticationError(_INVALID_CREDENTIALS)

            if not await self._verify_password(password, user.password_hash):
                raise AuthenticationError(_INVALID_CREDENTIALS)

            # Checked *after* verification so a disabled account costs the same
            # as an enabled one; checking first would leak which emails exist.
            if not user.is_active:
                raise AuthenticationError(_INVALID_CREDENTIALS)

            if self._password_hasher.needs_rehash(user.password_hash):
                # The only moment the plaintext is available, so the only chance
                # to upgrade a hash made with weaker parameters. Committed below
                # with the rest of the login.
                user.password_hash = await self._hash_password(password)
                log.info("password_rehashed", user=user.public_id)

            # A login starts a new rotation lineage; every token later issued by
            # refreshing this session inherits the family id.
            tokens = await self._issue_token_pair(uow, user, family_id=new_public_id())

            await uow.commit()

        log.info("user_logged_in", user=user.public_id)
        return tokens

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, retiring the one presented.

        Rotation means a refresh token is single-use. Presenting one that has
        already been rotated is therefore not a mistake but evidence that two
        parties hold it, so the entire family — every session descended from
        that login — is revoked (ADR-010).

        Raises :class:`AuthenticationError`, with one identical message, for
        every failure.
        """

        claims = self._decode_refresh_token(refresh_token)
        now = datetime.now(UTC)

        async with self._unit_of_work_factory() as uow:
            # The lock is taken before anything is read, and held to commit.
            # See the module note on concurrency for why this is what makes
            # rotation safe against a concurrent second use.
            stored = await uow.refresh_tokens.get_by_jti(claims.jti, for_update=True)

            if stored is None:
                raise AuthenticationError(_INVALID_REFRESH)

            # Checked before the replay test, so a token that merely *claims*
            # this jti cannot trigger a family revocation. Constant-time.
            if not verify_token_hash(refresh_token, stored.token_hash):
                raise AuthenticationError(_INVALID_REFRESH)

            if stored.revoked_at is not None:
                revoked = await uow.refresh_tokens.revoke_family(stored.family_id, now)
                # This commit is essential, not incidental: the raise below
                # unwinds through `__aexit__`, which rolls back. Without
                # committing first, detection would happen and then be
                # discarded, leaving the attacker's token live.
                await uow.commit()
                log.warning(
                    "refresh_token_reuse_detected",
                    family=stored.family_id,
                    user_id=stored.user_id,
                    revoked_tokens=revoked,
                )
                raise AuthenticationError(_INVALID_REFRESH)

            # The database is authoritative about expiry, not the token. They
            # agree today, but a future "end all sessions" action would work by
            # shortening this column, and that must be honoured immediately.
            if _as_utc(stored.expires_at) <= now:
                raise AuthenticationError(_INVALID_REFRESH)

            # Re-read rather than trusting the token's claims: this is where a
            # deleted or disabled account stops being able to extend itself, and
            # where a role change takes effect.
            user = await uow.users.get_by_id(stored.user_id)
            if user is None or not user.is_active:
                raise AuthenticationError(_INVALID_REFRESH)

            await uow.refresh_tokens.revoke(stored, now)
            # Same transaction as the revocation above, so there is no instant
            # at which the old token is dead and no successor exists.
            tokens = await self._issue_token_pair(uow, user, family_id=stored.family_id)

            await uow.commit()

        log.info("token_refreshed", user=user.public_id, family=stored.family_id)
        return tokens

    async def logout(self, refresh_token: str) -> None:
        """End the session the token belongs to, and every token descended from it.

        Revoking the whole family rather than one token is what makes logout
        mean "this session is over": the successor a client may already hold
        would otherwise keep working.

        Idempotent. Logging out twice succeeds, and unlike :meth:`refresh` an
        already-revoked token is *not* treated as a replay — a client repeating
        a logout is asking for a state that already holds.

        Raises :class:`AuthenticationError` only if the token is not a valid,
        unexpired refresh token.
        """

        claims = self._decode_refresh_token(refresh_token)

        async with self._unit_of_work_factory() as uow:
            # No row lock: `revoke_family` is a single UPDATE, atomic on its
            # own, and two concurrent logouts of the same family are harmless —
            # the second simply matches nothing left to revoke.
            stored = await uow.refresh_tokens.get_by_jti(claims.jti)
            if stored is not None:
                revoked = await uow.refresh_tokens.revoke_family(
                    stored.family_id, datetime.now(UTC)
                )
                log.info("user_logged_out", user_id=stored.user_id, revoked_tokens=revoked)
            # An unknown jti still succeeds. Reporting it would tell a caller
            # which tokens the server has ever issued, and there is nothing to
            # undo either way.
            await uow.commit()

    async def forgot_password(self, *, email: str) -> None:
        """Issue a reset link for ``email``, if that address can receive one.

        Returns ``None`` in every case, including the ones where nothing
        happened. That is the whole design: the caller is told the request was
        accepted and never whether it did anything, so the endpoint cannot be
        used to test which addresses have accounts.

        Issuing a link **supersedes** any the user already holds. Only the
        newest link works, which bounds a token's real lifetime by the next
        request as well as by its own expiry, and removes the question of which
        of three emails in an inbox is the live one.

        Raises nothing a client can see. A delivery failure is logged and
        swallowed, because surfacing it would confirm the address exists.
        """

        normalized_email = self._normalize_email(email)

        async with self._unit_of_work_factory() as uow:
            user = await uow.users.get_by_email(normalized_email)

            # A disabled account is treated exactly as a missing one. Letting a
            # deactivated user reset their way back in would make deactivation
            # advisory, and answering differently would leak that the address is
            # known.
            if user is None or not user.is_active:
                return

            now = datetime.now(UTC)
            # Supersede first, then insert: the new grant must not be caught by
            # the bulk consume that retires the old ones. Both statements are in
            # this transaction, so there is no instant at which the user has no
            # usable link because the old ones died before the new one existed.
            superseded = await uow.password_reset_tokens.consume_outstanding_for_user(user.id, now)

            token = new_password_reset_token()
            await uow.password_reset_tokens.add(
                PasswordResetToken(
                    user_id=user.id,
                    # Only the digest is stored; the token itself is never
                    # persisted, exactly as with refresh tokens.
                    token_hash=hash_token(token),
                    expires_at=now + timedelta(seconds=self._password_reset_ttl_seconds),
                )
            )

            await uow.commit()
            recipient = user.email
            user_public_id = user.public_id

        log.info("password_reset_requested", user=user_public_id, superseded_tokens=superseded)

        # Delivered *after* the commit and outside the transaction: a slow or
        # unreachable provider must not hold a database lock, and a grant that
        # was written but not delivered is recoverable by asking again, whereas
        # one delivered but not written is a link that would never work.
        await self._deliver_reset_link(recipient, token)

    async def reset_password(self, *, token: str, new_password: str) -> None:
        """Redeem a reset link and set a new password.

        Every session the user has ends. That is the point of the operation
        rather than a side effect: someone resets a password because the old one
        may be known to another party, and leaving that party's refresh tokens
        live would make the reset cosmetic.

        Raises :class:`AuthenticationError`, with one identical message, for
        every way this can fail.
        """

        digest = hash_token(token)
        now = datetime.now(UTC)

        async with self._unit_of_work_factory() as uow:
            # Locked for the same reason rotation locks: two requests carrying
            # the same link must not both succeed. The row is found through a
            # unique index, so this is a single record lock, and the second
            # request blocks until the first commits and then sees `consumed_at`
            # set. See the module note on concurrency.
            grant = await uow.password_reset_tokens.get_by_digest(digest, for_update=True)

            if grant is None:
                raise AuthenticationError(_INVALID_RESET)

            # Consumed covers both "already used" and "superseded by a newer
            # request" — one column, so neither case can be forgotten here.
            if grant.consumed_at is not None:
                raise AuthenticationError(_INVALID_RESET)

            if _as_utc(grant.expires_at) <= now:
                raise AuthenticationError(_INVALID_RESET)

            user = await uow.users.get_by_id(grant.user_id)
            if user is None or not user.is_active:
                raise AuthenticationError(_INVALID_RESET)

            user.password_hash = await self._hash_password(new_password)
            await uow.password_reset_tokens.consume(grant, now)
            # Any *other* outstanding grant dies too. Without this, a user who
            # asked twice could reset again from the older mail after the newer
            # one had already been used.
            await uow.password_reset_tokens.consume_outstanding_for_user(user.id, now)
            revoked = await uow.refresh_tokens.revoke_all_for_user(user.id, now)

            # One transaction: a reset that changed the password but left the
            # sessions live, or consumed the grant without changing anything,
            # are both worse than a reset that did not happen.
            await uow.commit()
            user_public_id = user.public_id

        log.info("password_reset_completed", user=user_public_id, revoked_sessions=revoked)

    # --- Internals ----------------------------------------------------------

    async def _deliver_reset_link(self, recipient: str, token: str) -> None:
        """Hand the finished link to the notifier, absorbing every failure.

        Nothing here may raise. This runs after the grant is committed, and the
        caller's contract is to answer identically whether or not an account
        exists — an exception escaping would turn a delivery outage into a 500
        that only ever happens for addresses that *do* have accounts, which is
        the enumeration oracle rebuilt by accident.
        """

        if self._password_reset_url_base is None:
            # No configured destination: the grant exists and is unreachable.
            # Logged as a warning because it is an operational fault, not a
            # user error, and it is invisible from outside by design.
            log.warning("password_reset_url_base_not_configured")
            return

        # The token is the entire proof, so the link carries nothing else — no
        # email, no user id, no organization id. A URL that identified the
        # account would leak it to every proxy, mail scanner, and browser
        # history the message passes through.
        reset_url = f"{self._password_reset_url_base}?token={quote(token, safe='')}"

        try:
            await self._password_reset_notifier.send_password_reset(recipient, reset_url)
        except PasswordResetDeliveryError:
            # Deliberately not re-raised, and deliberately logged without the
            # recipient or the URL.
            log.warning("password_reset_delivery_failed")

    @staticmethod
    def _normalize_email(email: str) -> str:
        """Trim and lower-case, so the stored form matches how it is compared.

        The column's collation is already case-insensitive, so this changes no
        lookup result; it keeps the *stored* value canonical regardless of which
        caller supplied it.
        """

        return email.strip().lower()

    @staticmethod
    def _to_authenticated_user(user: User) -> AuthenticatedUser:
        """Project a persisted user onto the identity the token will assert.

        Relies on ``organization`` and ``user_roles`` already being loaded — the
        user repository eager-loads both precisely for this.
        """

        return AuthenticatedUser(
            public_id=user.public_id,
            organization_id=user.organization.public_id,
            roles=frozenset(assignment.role.name for assignment in user.user_roles),
        )

    def _decode_refresh_token(self, refresh_token: str) -> TokenClaims:
        """Verify a refresh token, reporting every failure identically.

        The token service raises its own ``AuthenticationError`` with a message
        about tokens in general, while the checks below this speak about refresh
        tokens. Left alone, that difference is an oracle: a caller could tell
        "this failed signature or expiry verification" apart from "this verified
        but the server refused it", which reveals whether a stolen token is
        still cryptographically live. Re-raising with the single message closes
        that; the original is chained for the server's own logs.
        """

        try:
            claims = self._token_service.decode(refresh_token)
        except AuthenticationError as exc:
            raise AuthenticationError(_INVALID_REFRESH) from exc

        # An access token is signed by the same key and would otherwise verify
        # here, letting a leaked access token mint long-lived refresh tokens.
        if claims.token_type is not TokenType.REFRESH:
            raise AuthenticationError(_INVALID_REFRESH)

        return claims

    async def _issue_token_pair(
        self,
        uow: SqlAlchemyUnitOfWork,
        user: User,
        *,
        family_id: str,
    ) -> TokenPair:
        """Mint an access/refresh pair and record the refresh token.

        Shared by login and refresh so the two cannot drift: the only thing that
        differs between starting a session and extending one is whether the
        family id is new or inherited.

        Does not commit — the caller owns the transaction.
        """

        authenticated = self._to_authenticated_user(user)
        access = self._token_service.create_access_token(authenticated)
        refresh = self._token_service.create_refresh_token(authenticated)

        await uow.refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                jti=refresh.claims.jti,
                # Only the digest is stored; the token itself is never persisted.
                token_hash=hash_token(refresh.token),
                family_id=family_id,
                # Taken from the token's own claims, so the row and the
                # credential expire at exactly the same instant.
                expires_at=refresh.claims.expires_at,
            )
        )

        return TokenPair(access_token=access.token, refresh_token=refresh.token)

    async def _hash_password(self, password: str) -> str:
        """Hash off the event loop.

        Argon2 is deliberately expensive (tens of milliseconds of pure CPU).
        Run inline it would stall every other request on the worker for that
        long, so the whole process would serialize behind logins. The port is
        synchronous exactly so the caller can make this choice.
        """

        return await asyncio.to_thread(self._password_hasher.hash_password, password)

    async def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify off the event loop, for the same reason as :meth:`_hash_password`."""

        return await asyncio.to_thread(
            self._password_hasher.verify_password, password, password_hash
        )

    @staticmethod
    async def _available_slug(uow: SqlAlchemyUnitOfWork, organization_name: str) -> str:
        """Find a free slug for ``organization_name``.

        Tries the plain slug, then a handful of numbered variants, then gives up
        guessing and appends a ULID. The fallback is what makes this terminate:
        organization names collide often ("Acme"), and probing indefinitely
        would turn a popular name into an unbounded number of queries.
        """

        base = _slugify(organization_name)
        if not await uow.organizations.slug_exists(base):
            return base

        for suffix in range(2, 2 + _MAX_SLUG_ATTEMPTS):
            candidate = f"{base}-{suffix}"
            if not await uow.organizations.slug_exists(candidate):
                return candidate

        return f"{base}-{new_public_id().lower()}"
