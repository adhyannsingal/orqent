"""AuthService use cases, against in-memory doubles (no database).

The doubles enforce the same uniqueness rules the schema does and discard
pending work on rollback, so these tests exercise real behaviour rather than
call recording.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app.domain.errors import AuthenticationError, ConflictError, InfrastructureError
from app.domain.value_objects.token import TokenType
from app.domain.value_objects.token_pair import TokenPair
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.password_reset_token import PASSWORD_RESET_TOKEN_LENGTH
from app.infrastructure.security.token_hashing import hash_token
from app.services.auth_service import _DUMMY_PASSWORD_HASH, DEFAULT_ROLE, AuthService, _slugify
from tests.unit.fakes import (
    FakeDatabase,
    FakePasswordHasher,
    FakePasswordResetNotifier,
    FakeTokenService,
    FakeUnitOfWorkFactory,
    FakeUserRepository,
    integrity_error,
)

EMAIL = "founder@example.com"
PASSWORD = "correct horse battery staple"
ORGANIZATION = "Acme Inc"


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    database.add_role(DEFAULT_ROLE)
    return database


@pytest.fixture
def hasher() -> FakePasswordHasher:
    return FakePasswordHasher()


@pytest.fixture
def tokens() -> FakeTokenService:
    return FakeTokenService()


@pytest.fixture
def factory(db: FakeDatabase) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(db)


RESET_URL_BASE = "https://app.example.com/reset-password"
RESET_TTL_SECONDS = 1_800


@pytest.fixture
def notifier() -> FakePasswordResetNotifier:
    return FakePasswordResetNotifier()


def build_service(
    factory: FakeUnitOfWorkFactory,
    hasher: FakePasswordHasher,
    tokens: FakeTokenService,
    notifier: FakePasswordResetNotifier | None = None,
    *,
    reset_url_base: str | None = RESET_URL_BASE,
) -> AuthService:
    return AuthService(
        factory,
        hasher,
        tokens,
        notifier or FakePasswordResetNotifier(),
        password_reset_ttl_seconds=RESET_TTL_SECONDS,
        password_reset_url_base=reset_url_base,
    )


@pytest.fixture
def service(
    factory: FakeUnitOfWorkFactory,
    hasher: FakePasswordHasher,
    tokens: FakeTokenService,
    notifier: FakePasswordResetNotifier,
) -> AuthService:
    return build_service(factory, hasher, tokens, notifier)


async def _register(service: AuthService, **overrides: str) -> User:
    payload = {"email": EMAIL, "password": PASSWORD, "organization_name": ORGANIZATION}
    payload.update(overrides)
    return await service.register(**payload)


# --- Registration -----------------------------------------------------------


async def test_register_creates_user_organization_and_assignment(
    service: AuthService, db: FakeDatabase
) -> None:
    user = await _register(service)

    assert user.email == EMAIL
    assert len(db.organizations) == 1
    assert len(db.users) == 1
    assert len(db.user_roles) == 1


async def test_register_creates_the_organization_with_the_given_name(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)

    organization = db.organizations[0]
    assert organization.name == ORGANIZATION
    assert organization.slug == "acme-inc"


async def test_register_grants_the_owner_role(service: AuthService, db: FakeDatabase) -> None:
    await _register(service)

    assert db.user_roles[0].role.name == DEFAULT_ROLE


async def test_register_stores_a_hash_from_the_port_not_the_password(
    service: AuthService, db: FakeDatabase, hasher: FakePasswordHasher
) -> None:
    # That the hash is irreversible is the adapter's guarantee, covered in
    # test_password_hasher; what matters here is that the service routes the
    # password through the port and stores only what came back.
    await _register(service)

    assert hasher.hashed == [PASSWORD]
    assert db.users[0].password_hash == f"hashed::{PASSWORD}"


async def test_register_links_the_user_to_the_organization(
    service: AuthService, db: FakeDatabase
) -> None:
    user = await _register(service)

    assert user.organization is db.organizations[0]
    assert user.organization_id == db.organizations[0].id


async def test_registered_user_is_returned_with_roles_loaded(service: AuthService) -> None:
    # The API layer serializes this outside the session; a relationship that was
    # never loaded would raise there rather than here.
    user = await _register(service)

    assert {assignment.role.name for assignment in user.user_roles} == {DEFAULT_ROLE}


async def test_register_normalizes_the_email(service: AuthService, db: FakeDatabase) -> None:
    await _register(service, email="  Founder@Example.COM  ")

    assert db.users[0].email == EMAIL


async def test_register_rejects_a_duplicate_email(service: AuthService) -> None:
    await _register(service)

    with pytest.raises(ConflictError):
        await _register(service, organization_name="Second Co")


async def test_duplicate_email_leaves_nothing_behind(
    service: AuthService, db: FakeDatabase
) -> None:
    # The duplicate check happens before any write, so the second attempt must
    # not leave a stray organization.
    await _register(service)

    with pytest.raises(ConflictError):
        await _register(service, organization_name="Second Co")

    assert len(db.organizations) == 1
    assert len(db.users) == 1


async def test_register_fails_when_the_role_catalog_is_not_seeded(
    factory: FakeUnitOfWorkFactory, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # A deployment that skipped the seeding migration, not a client error.
    empty = FakeDatabase()
    service = build_service(FakeUnitOfWorkFactory(empty), hasher, tokens)

    with pytest.raises(InfrastructureError, match=DEFAULT_ROLE):
        await _register(service)


async def test_register_uses_one_transaction_and_commits_once(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    await _register(service)

    assert len(factory.created) == 1
    assert factory.only.entered == 1
    assert factory.only.commit_calls == 1


# --- Registration: slugs ----------------------------------------------------


async def test_second_organization_with_the_same_name_gets_a_suffixed_slug(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)
    await _register(service, email="second@example.com")

    assert [o.slug for o in db.organizations] == ["acme-inc", "acme-inc-2"]


async def test_slug_falls_back_to_a_unique_suffix_when_numbering_is_exhausted(
    service: AuthService, db: FakeDatabase
) -> None:
    # Probing forever would make a popular name cost unbounded queries.
    for index in range(7):
        await _register(service, email=f"user{index}@example.com")

    slugs = [o.slug for o in db.organizations]
    assert slugs[:6] == [
        "acme-inc",
        "acme-inc-2",
        "acme-inc-3",
        "acme-inc-4",
        "acme-inc-5",
        "acme-inc-6",
    ]
    assert slugs[6].startswith("acme-inc-")
    assert len(slugs) == len(set(slugs))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Inc", "acme-inc"),
        ("  Spaced  Out  ", "spaced-out"),
        ("Café Zürich", "cafe-zurich"),  # accents folded, not dropped
        ("A/B  Testing!", "a-b-testing"),
        ("--dashes--", "dashes"),
        ("!!!", "org"),  # nothing usable survives
        ("日本語", "org"),  # no ASCII equivalent
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert _slugify(name) == expected


def test_slugify_bounds_the_length_to_leave_room_for_a_suffix() -> None:
    assert len(_slugify("x" * 500)) == 200


# --- Registration: failure and rollback -------------------------------------


async def test_concurrent_duplicate_becomes_a_conflict_error(
    db: FakeDatabase, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # Simulates another request claiming the email between the pre-check and the
    # insert: the database refuses, and the service reports it in domain terms
    # rather than leaking IntegrityError.
    failing = FakeUserRepository(db, raise_on_add=integrity_error("uq_users_email_active"))
    factory = FakeUnitOfWorkFactory(db, user_repository=failing)
    service = build_service(factory, hasher, tokens)

    with pytest.raises(ConflictError):
        await _register(service)


async def test_failed_registration_rolls_everything_back(
    db: FakeDatabase, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # The organization is written before the user, so a later failure must not
    # leave an orphan tenant behind.
    failing = FakeUserRepository(db, raise_on_add=integrity_error("uq_users_email_active"))
    factory = FakeUnitOfWorkFactory(db, user_repository=failing)
    service = build_service(factory, hasher, tokens)

    with pytest.raises(ConflictError):
        await _register(service)

    assert db.organizations == []
    assert db.users == []
    assert db.user_roles == []
    assert factory.only.commit_calls == 0
    assert factory.only.rollback_calls == 1


async def test_unexpected_failure_propagates_and_rolls_back(
    db: FakeDatabase, hasher: FakePasswordHasher, tokens: FakeTokenService
) -> None:
    # Only integrity errors are translated; anything else is a bug and must not
    # be disguised as a conflict.
    failing = FakeUserRepository(db, raise_on_add=RuntimeError("boom"))
    factory = FakeUnitOfWorkFactory(db, user_repository=failing)
    service = build_service(factory, hasher, tokens)

    with pytest.raises(RuntimeError, match="boom"):
        await _register(service)

    assert db.organizations == []
    assert factory.only.commit_calls == 0


# --- Login ------------------------------------------------------------------


async def test_login_returns_a_token_pair(service: AuthService) -> None:
    await _register(service)

    pair = await service.login(email=EMAIL, password=PASSWORD)

    assert isinstance(pair, TokenPair)
    assert pair.access_token
    assert pair.refresh_token
    assert pair.access_token != pair.refresh_token


async def test_login_issues_one_access_and_one_refresh_token(
    service: AuthService, tokens: FakeTokenService
) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    assert [issued.claims.token_type for issued in tokens.issued] == [
        TokenType.ACCESS,
        TokenType.REFRESH,
    ]


async def test_login_puts_the_users_identity_in_the_token(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    claims = tokens.issued[0].claims
    assert claims.subject == db.users[0].public_id
    assert claims.organization_id == db.organizations[0].public_id
    assert claims.roles == frozenset({DEFAULT_ROLE})


async def test_login_accepts_a_differently_cased_email(service: AuthService) -> None:
    await _register(service)

    assert await service.login(email="  FOUNDER@EXAMPLE.com ", password=PASSWORD)


async def test_login_rejects_an_unknown_email(service: AuthService) -> None:
    with pytest.raises(AuthenticationError):
        await service.login(email="nobody@example.com", password=PASSWORD)


async def test_login_failure_carries_the_401_status_whichever_check_failed(
    service: AuthService,
) -> None:
    # Both paths must reach the API layer as the same error class, or the
    # envelope's status and code would differ even with identical wording.
    await _register(service)

    for email, password in (("nobody@example.com", PASSWORD), (EMAIL, "wrong")):
        with pytest.raises(AuthenticationError) as caught:
            await service.login(email=email, password=password)
        assert caught.value.http_status == 401
        assert caught.value.code == "authentication_error"
        assert caught.value.details == []


async def test_login_rejects_a_wrong_password(service: AuthService) -> None:
    await _register(service)

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password="not the password")


async def test_login_rejects_an_inactive_user(service: AuthService, db: FakeDatabase) -> None:
    await _register(service)
    db.users[0].is_active = False

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password=PASSWORD)


async def test_every_login_failure_reports_the_same_message(
    service: AuthService, db: FakeDatabase
) -> None:
    # Distinguishable messages would turn the login form into an oracle for
    # which addresses have accounts.
    await _register(service)
    messages = set()

    for email, password in (
        ("nobody@example.com", PASSWORD),
        (EMAIL, "wrong"),
    ):
        with pytest.raises(AuthenticationError) as caught:
            await service.login(email=email, password=password)
        messages.add(caught.value.message)

    db.users[0].is_active = False
    with pytest.raises(AuthenticationError) as caught:
        await service.login(email=EMAIL, password=PASSWORD)
    messages.add(caught.value.message)

    # Pinned to the literal, not to `_INVALID_CREDENTIALS`. Asserting only that
    # the three agree passes just as happily when all three say "user not
    # found" — agreement is not the property under test, saying nothing is.
    assert messages == {"Either email or password is incorrect."}


# --- Login: timing ----------------------------------------------------------


async def test_unknown_email_still_performs_a_verification(
    service: AuthService, hasher: FakePasswordHasher
) -> None:
    # Without this, "no such account" returns in microseconds while a real
    # attempt takes ~80 ms, and the difference is measurable over the network.
    with pytest.raises(AuthenticationError):
        await service.login(email="nobody@example.com", password=PASSWORD)

    assert hasher.verified == [(PASSWORD, _DUMMY_PASSWORD_HASH)]


async def test_inactive_user_is_checked_after_verifying_the_password(
    service: AuthService, db: FakeDatabase, hasher: FakePasswordHasher
) -> None:
    # Short-circuiting on is_active would make a disabled account cheaper than
    # an enabled one, leaking that the address exists.
    await _register(service)
    db.users[0].is_active = False
    hasher.verified.clear()

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password=PASSWORD)

    assert hasher.verified == [(PASSWORD, f"hashed::{PASSWORD}")]


def test_dummy_hash_is_a_real_argon2_hash() -> None:
    # If it were malformed, verification would fail instantly instead of doing
    # the work, defeating the whole point of the constant.
    assert Argon2PasswordHasher().verify_password("anything", _DUMMY_PASSWORD_HASH) is False


def test_dummy_hash_uses_current_parameters() -> None:
    # Guards the timing defence: a dummy built with cheaper parameters than the
    # library's current defaults would be faster than a real verification.
    assert Argon2PasswordHasher().needs_rehash(_DUMMY_PASSWORD_HASH) is False


# --- Login: rehash ----------------------------------------------------------


async def test_login_rehashes_when_the_stored_hash_is_outdated(
    factory: FakeUnitOfWorkFactory, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    upgrading = FakePasswordHasher(needs_rehash=True)
    service = build_service(factory, upgrading, tokens)
    await _register(service)
    upgrading.hashed.clear()

    await service.login(email=EMAIL, password=PASSWORD)

    # Login is the only moment the plaintext is available, so it is the only
    # chance to upgrade the stored hash.
    assert upgrading.hashed == [PASSWORD]
    assert db.users[0].password_hash == f"hashed::{PASSWORD}"


async def test_login_does_not_rehash_when_the_hash_is_current(
    service: AuthService, hasher: FakePasswordHasher
) -> None:
    await _register(service)
    hasher.hashed.clear()

    await service.login(email=EMAIL, password=PASSWORD)

    assert hasher.hashed == []


# --- Login: refresh token persistence ---------------------------------------


async def test_login_persists_exactly_one_refresh_token(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    assert len(db.refresh_tokens) == 1


async def test_persisted_refresh_token_matches_the_issued_token(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    await _register(service)

    pair = await service.login(email=EMAIL, password=PASSWORD)

    stored = db.refresh_tokens[0]
    refresh_claims = tokens.issued[1].claims
    assert stored.jti == refresh_claims.jti
    assert stored.user_id == db.users[0].id
    # The row and the credential must expire at the same instant, or one
    # outlives the other.
    assert stored.expires_at == refresh_claims.expires_at
    assert stored.token_hash == hash_token(pair.refresh_token)


async def test_the_refresh_token_itself_is_never_stored(
    service: AuthService, db: FakeDatabase
) -> None:
    await _register(service)

    pair = await service.login(email=EMAIL, password=PASSWORD)

    stored = db.refresh_tokens[0]
    assert pair.refresh_token not in stored.token_hash
    assert stored.token_hash != pair.refresh_token


async def test_stored_refresh_token_starts_live(service: AuthService, db: FakeDatabase) -> None:
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)

    assert db.refresh_tokens[0].revoked_at is None


async def test_each_login_starts_a_new_token_family(service: AuthService, db: FakeDatabase) -> None:
    # Families exist so that revoking one compromised session does not end the
    # user's other sessions.
    await _register(service)

    await service.login(email=EMAIL, password=PASSWORD)
    await service.login(email=EMAIL, password=PASSWORD)

    families = {token.family_id for token in db.refresh_tokens}
    assert len(families) == 2


# --- Login: transaction boundaries ------------------------------------------


async def test_login_uses_one_transaction_and_commits_once(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    await _register(service)
    factory.created.clear()

    await service.login(email=EMAIL, password=PASSWORD)

    assert len(factory.created) == 1
    assert factory.created[0].commit_calls == 1


async def test_failed_login_commits_nothing(
    service: AuthService, factory: FakeUnitOfWorkFactory, db: FakeDatabase
) -> None:
    await _register(service)
    factory.created.clear()

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password="wrong")

    assert factory.created[0].commit_calls == 0
    assert factory.created[0].rollback_calls == 1
    assert db.refresh_tokens == []


# --- Layering ---------------------------------------------------------------


def test_service_module_imports_no_security_vendor() -> None:
    # Run in a fresh interpreter: another test importing the adapters would
    # otherwise make jwt/argon2 appear in sys.modules and hide a real leak.
    program = textwrap.dedent(
        """
        import sys
        import app.services.auth_service  # noqa: F401
        leaked = sorted(m for m in sys.modules if m.split(".")[0] in {"jwt", "argon2"})
        print(",".join(leaked))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == ""


def test_service_knows_nothing_about_http() -> None:
    # Business code raises domain errors; only the API layer knows status codes.
    source = inspect.getsource(sys.modules[AuthService.__module__])

    assert "HTTPException" not in source
    assert "fastapi" not in source


# --- Refresh: rotation ------------------------------------------------------


async def _login(service: AuthService) -> TokenPair:
    await _register(service)
    return await service.login(email=EMAIL, password=PASSWORD)


async def test_refresh_returns_a_new_pair(service: AuthService) -> None:
    original = await _login(service)

    rotated = await service.refresh(original.refresh_token)

    assert isinstance(rotated, TokenPair)
    assert rotated.access_token != original.access_token
    assert rotated.refresh_token != original.refresh_token


async def test_refresh_revokes_the_presented_token(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    presented = db.refresh_tokens[0]

    await service.refresh(original.refresh_token)

    assert presented.revoked_at is not None


async def test_refresh_stores_a_successor_in_the_same_family(
    service: AuthService, db: FakeDatabase
) -> None:
    # The lineage is what lets one compromised session be revoked wholesale.
    original = await _login(service)
    family = db.refresh_tokens[0].family_id

    await service.refresh(original.refresh_token)

    assert len(db.refresh_tokens) == 2
    assert {token.family_id for token in db.refresh_tokens} == {family}
    assert db.refresh_tokens[1].revoked_at is None


async def test_successor_hash_matches_the_returned_token(
    service: AuthService, db: FakeDatabase
) -> None:
    original = await _login(service)

    rotated = await service.refresh(original.refresh_token)

    assert db.refresh_tokens[1].token_hash == hash_token(rotated.refresh_token)


async def test_refresh_picks_up_a_role_change(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    # Access tokens carry roles, so a revoked or granted role only takes effect
    # when a new one is minted. Refresh re-reads the user for exactly this.
    original = await _login(service)
    admin = db.add_role("admin")
    UserRole(user=db.users[0], role=admin)  # backref populates user.user_roles

    await service.refresh(original.refresh_token)

    assert tokens.issued[-1].claims.roles == frozenset({DEFAULT_ROLE, "admin"})


async def test_refresh_uses_one_transaction(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    original = await _login(service)
    factory.created.clear()

    await service.refresh(original.refresh_token)

    assert len(factory.created) == 1
    assert factory.created[0].commit_calls == 1


# --- Refresh: rejection -----------------------------------------------------


async def test_refresh_rejects_an_access_token(service: AuthService) -> None:
    # Signed by the same key, so only the token_type claim stops it.
    original = await _login(service)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.access_token)


async def test_refresh_rejects_an_unverifiable_token(service: AuthService) -> None:
    await _login(service)

    with pytest.raises(AuthenticationError):
        await service.refresh("not-a-token")


async def test_refresh_rejects_a_token_with_no_stored_row(
    service: AuthService, db: FakeDatabase
) -> None:
    original = await _login(service)
    db.refresh_tokens.clear()

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_refresh_rejects_a_hash_mismatch(service: AuthService, db: FakeDatabase) -> None:
    # Would mean the presented token is not the one this row recorded.
    original = await _login(service)
    db.refresh_tokens[0].token_hash = hash_token("a different token")

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_hash_mismatch_does_not_revoke_the_family(
    service: AuthService, db: FakeDatabase
) -> None:
    # Checked before the replay test precisely so a token merely claiming this
    # jti cannot trigger a family-wide revocation.
    original = await _login(service)
    db.refresh_tokens[0].token_hash = hash_token("a different token")

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert db.refresh_tokens[0].revoked_at is None


async def test_refresh_rejects_a_row_that_has_expired(
    service: AuthService, db: FakeDatabase
) -> None:
    # The database is authoritative even when the token itself still verifies.
    original = await _login(service)
    db.refresh_tokens[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_refresh_handles_a_naive_stored_expiry(
    service: AuthService, db: FakeDatabase
) -> None:
    # MySQL DATETIME returns naive values; comparing one against an aware `now`
    # would raise TypeError and surface as a 500 rather than a refusal.
    original = await _login(service)
    db.refresh_tokens[0].expires_at = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)

    assert await service.refresh(original.refresh_token)


async def test_refresh_rejects_a_deleted_user(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    db.users[0].deleted_at = datetime.now(UTC)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_refresh_rejects_an_inactive_user(service: AuthService, db: FakeDatabase) -> None:
    # Disabling an account must stop it extending itself.
    original = await _login(service)
    db.users[0].is_active = False

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_failed_refresh_issues_no_tokens(
    service: AuthService, db: FakeDatabase, tokens: FakeTokenService
) -> None:
    original = await _login(service)
    db.users[0].is_active = False
    issued_before = len(tokens.issued)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert len(tokens.issued) == issued_before
    assert len(db.refresh_tokens) == 1


async def test_every_refresh_failure_reports_the_same_message(
    service: AuthService, db: FakeDatabase
) -> None:
    original = await _login(service)
    messages = set()

    with pytest.raises(AuthenticationError) as caught:
        await service.refresh(original.access_token)
    messages.add(caught.value.message)

    with pytest.raises(AuthenticationError) as caught:
        await service.refresh("nonsense")
    messages.add(caught.value.message)

    db.users[0].is_active = False
    with pytest.raises(AuthenticationError) as caught:
        await service.refresh(original.refresh_token)
    messages.add(caught.value.message)

    assert len(messages) == 1


# --- Refresh: reuse detection -----------------------------------------------


async def test_replaying_a_rotated_token_is_rejected(service: AuthService) -> None:
    original = await _login(service)
    await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)


async def test_replay_revokes_the_whole_family(service: AuthService, db: FakeDatabase) -> None:
    # The attacker's successor must die with the replayed parent, otherwise a
    # thief simply keeps rotating and the victim is silently logged out.
    original = await _login(service)
    await service.refresh(original.refresh_token)
    assert db.refresh_tokens[1].revoked_at is None

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert all(token.revoked_at is not None for token in db.refresh_tokens)


async def test_replay_commits_the_family_revocation(
    service: AuthService, factory: FakeUnitOfWorkFactory
) -> None:
    # The raise unwinds through the unit of work's rollback, so detection is
    # only durable if it commits first.
    original = await _login(service)
    await service.refresh(original.refresh_token)
    factory.created.clear()

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    assert factory.created[0].commit_calls == 1


async def test_replay_leaves_the_successor_unusable(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    rotated = await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(rotated.refresh_token)


async def test_replay_does_not_touch_another_family(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    other = await service.login(email=EMAIL, password=PASSWORD)
    await service.refresh(original.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(original.refresh_token)

    # The user's other session is unaffected.
    assert await service.refresh(other.refresh_token)


# --- Logout -----------------------------------------------------------------


async def test_logout_revokes_the_whole_family(service: AuthService, db: FakeDatabase) -> None:
    original = await _login(service)
    await service.refresh(original.refresh_token)

    await service.logout(original.refresh_token)

    assert all(token.revoked_at is not None for token in db.refresh_tokens)


async def test_logout_stops_further_refreshing(service: AuthService) -> None:
    original = await _login(service)
    rotated = await service.refresh(original.refresh_token)

    await service.logout(rotated.refresh_token)

    with pytest.raises(AuthenticationError):
        await service.refresh(rotated.refresh_token)


async def test_logout_is_idempotent(service: AuthService) -> None:
    # A repeated logout asks for a state that already holds; unlike refresh, an
    # already-revoked token here is not evidence of a replay.
    original = await _login(service)

    await service.logout(original.refresh_token)
    await service.logout(original.refresh_token)


async def test_logout_of_an_unknown_token_succeeds(service: AuthService, db: FakeDatabase) -> None:
    # Reporting it would disclose which tokens the server has issued.
    original = await _login(service)
    db.refresh_tokens.clear()

    await service.logout(original.refresh_token)


async def test_logout_rejects_an_access_token(service: AuthService) -> None:
    original = await _login(service)

    with pytest.raises(AuthenticationError):
        await service.logout(original.access_token)


async def test_logout_rejects_an_unverifiable_token(service: AuthService) -> None:
    with pytest.raises(AuthenticationError):
        await service.logout("nonsense")


async def test_logout_leaves_other_sessions_alone(service: AuthService) -> None:
    first = await _login(service)
    second = await service.login(email=EMAIL, password=PASSWORD)

    await service.logout(first.refresh_token)

    assert await service.refresh(second.refresh_token)


async def test_logout_commits(service: AuthService, factory: FakeUnitOfWorkFactory) -> None:
    original = await _login(service)
    factory.created.clear()

    await service.logout(original.refresh_token)

    assert factory.created[0].commit_calls == 1


# --- Forgot password --------------------------------------------------------


def _issued_token(notifier: FakePasswordResetNotifier) -> str:
    """The raw token, recovered from the only place it legitimately appears."""

    _, url = notifier.sent[-1]
    return url.rsplit("token=", 1)[1]


async def test_forgot_password_issues_a_grant_for_a_known_address(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)

    await service.forgot_password(email=EMAIL)

    assert len(db.password_reset_tokens) == 1
    assert len(notifier.sent) == 1


async def test_forgot_password_is_silent_for_an_unknown_address(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)

    await service.forgot_password(email="nobody@example.com")

    # Nothing minted and nothing sent — and, critically, nothing raised: the
    # caller cannot tell this apart from the case above.
    assert db.password_reset_tokens == []
    assert notifier.sent == []


async def test_forgot_password_is_silent_for_a_disabled_account(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # Letting a deactivated user reset their way back in would make
    # deactivation advisory rather than enforced.
    await _register(service)
    db.users[0].is_active = False

    await service.forgot_password(email=EMAIL)

    assert db.password_reset_tokens == []
    assert notifier.sent == []


async def test_forgot_password_stores_a_digest_and_never_the_token(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # The single most important property of the table: a database leak must
    # yield no usable reset link.
    await _register(service)

    await service.forgot_password(email=EMAIL)

    token = _issued_token(notifier)
    stored = db.password_reset_tokens[0]
    assert stored.token_hash == hash_token(token)
    assert stored.token_hash != token
    assert token not in stored.token_hash


async def test_issued_token_is_high_entropy_and_url_safe(
    service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)

    await service.forgot_password(email=EMAIL)

    token = _issued_token(notifier)
    assert len(token) == PASSWORD_RESET_TOKEN_LENGTH
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


async def test_forgot_password_records_the_configured_expiry(
    service: AuthService, db: FakeDatabase
) -> None:
    before = datetime.now(UTC)

    await _register(service)
    await service.forgot_password(email=EMAIL)

    expires_at = db.password_reset_tokens[0].expires_at
    assert before + timedelta(seconds=RESET_TTL_SECONDS) <= expires_at
    assert expires_at <= datetime.now(UTC) + timedelta(seconds=RESET_TTL_SECONDS)


async def test_a_second_request_supersedes_the_first(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # Only the newest link works, which bounds a token's real lifetime by the
    # next request as well as by its own expiry.
    await _register(service)
    await service.forgot_password(email=EMAIL)
    first = _issued_token(notifier)

    await service.forgot_password(email=EMAIL)
    second = _issued_token(notifier)

    outstanding = [t for t in db.password_reset_tokens if t.consumed_at is None]
    assert [t.token_hash for t in outstanding] == [hash_token(second)]
    assert first != second


async def test_the_reset_link_identifies_nobody(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # The token is the entire proof, so the URL needs no identity — and must
    # carry none: it passes through mail servers, proxies, and browser history.
    await _register(service)

    await service.forgot_password(email=EMAIL)

    _, url = notifier.sent[0]
    user = db.users[0]
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    # The whole query string, not a substring search: `token` must be the only
    # parameter. Scanning for known identifiers would miss one nobody thought
    # of, and a bare numeric id is unsearchable anyway — "3" appears in a
    # base64url token by chance.
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == RESET_URL_BASE
    assert set(query) == {"token"}
    assert EMAIL not in url
    assert user.public_id not in url
    assert user.organization.public_id not in url


async def test_delivery_goes_to_the_accounts_own_address(
    service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    # Sent to the *stored* address, not the submitted one. They are equal after
    # normalisation here, and using the stored value is what keeps that true
    # when they are not.
    await _register(service)

    await service.forgot_password(email="  FOUNDER@EXAMPLE.com ")

    assert notifier.sent[0][0] == EMAIL


async def test_a_delivery_failure_is_absorbed(
    factory: FakeUnitOfWorkFactory,
    hasher: FakePasswordHasher,
    tokens: FakeTokenService,
    db: FakeDatabase,
) -> None:
    # Raising would turn a provider outage into a 500 that happens only for
    # addresses that do have accounts — the enumeration oracle rebuilt by
    # accident.
    failing = FakePasswordResetNotifier(fail=True)
    service = build_service(factory, hasher, tokens, failing)
    await _register(service)

    await service.forgot_password(email=EMAIL)

    # The grant is still committed: the user can ask again and the link works.
    assert len(db.password_reset_tokens) == 1


async def test_no_link_is_built_when_no_base_url_is_configured(
    factory: FakeUnitOfWorkFactory,
    hasher: FakePasswordHasher,
    tokens: FakeTokenService,
    db: FakeDatabase,
) -> None:
    notifier = FakePasswordResetNotifier()
    service = build_service(factory, hasher, tokens, notifier, reset_url_base=None)
    await _register(service)

    await service.forgot_password(email=EMAIL)

    # Nothing delivered, nothing raised, and the grant still written — the
    # endpoint stays enumeration-safe with no destination configured.
    assert notifier.sent == []
    assert len(db.password_reset_tokens) == 1


# --- Reset password ---------------------------------------------------------


async def _request_reset(service: AuthService, notifier: FakePasswordResetNotifier) -> str:
    await service.forgot_password(email=EMAIL)
    return _issued_token(notifier)


NEW_PASSWORD = "a whole new passphrase 9!"


async def test_reset_replaces_the_password_hash(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    before = db.users[0].password_hash
    token = await _request_reset(service, notifier)

    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    assert db.users[0].password_hash != before
    assert db.users[0].password_hash == f"hashed::{NEW_PASSWORD}"


async def test_reset_lets_the_new_password_log_in(
    service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    token = await _request_reset(service, notifier)

    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    assert await service.login(email=EMAIL, password=NEW_PASSWORD)


async def test_reset_stops_the_old_password_working(
    service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    token = await _request_reset(service, notifier)

    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    with pytest.raises(AuthenticationError):
        await service.login(email=EMAIL, password=PASSWORD)


async def test_reset_consumes_the_grant(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    token = await _request_reset(service, notifier)

    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    assert db.password_reset_tokens[0].consumed_at is not None


async def test_a_reset_token_cannot_be_used_twice(
    service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    token = await _request_reset(service, notifier)
    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    with pytest.raises(AuthenticationError):
        await service.reset_password(token=token, new_password="another one entirely 4?")


async def test_a_second_reset_does_not_change_the_password_again(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # The replay must be refused *and* inert; a rejection that still wrote
    # would be worse than no rejection at all.
    await _register(service)
    token = await _request_reset(service, notifier)
    await service.reset_password(token=token, new_password=NEW_PASSWORD)
    after_first = db.users[0].password_hash

    with pytest.raises(AuthenticationError):
        await service.reset_password(token=token, new_password="another one entirely 4?")

    assert db.users[0].password_hash == after_first


async def test_reset_rejects_an_unknown_token(service: AuthService) -> None:
    await _register(service)

    with pytest.raises(AuthenticationError):
        await service.reset_password(token="not a real token", new_password=NEW_PASSWORD)


async def test_reset_rejects_an_expired_token(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    token = await _request_reset(service, notifier)
    db.password_reset_tokens[0].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(AuthenticationError):
        await service.reset_password(token=token, new_password=NEW_PASSWORD)


async def test_reset_accepts_a_token_that_has_not_yet_expired(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # The other half of the boundary: without this, deleting the expiry check
    # and hard-expiring everything would both pass.
    await _register(service)
    token = await _request_reset(service, notifier)
    db.password_reset_tokens[0].expires_at = datetime.now(UTC) + timedelta(seconds=1)

    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    assert db.users[0].password_hash == f"hashed::{NEW_PASSWORD}"


async def test_reset_rejects_a_superseded_token(
    service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    first = await _request_reset(service, notifier)
    await _request_reset(service, notifier)

    with pytest.raises(AuthenticationError):
        await service.reset_password(token=first, new_password=NEW_PASSWORD)


async def test_reset_rejects_a_token_for_a_disabled_account(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    token = await _request_reset(service, notifier)
    db.users[0].is_active = False

    with pytest.raises(AuthenticationError):
        await service.reset_password(token=token, new_password=NEW_PASSWORD)


async def test_every_reset_failure_reports_the_same_message(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # Distinguishable messages would confirm to whoever holds a stale link that
    # it was once real.
    await _register(service)
    messages = set()

    with pytest.raises(AuthenticationError) as caught:
        await service.reset_password(token="never issued", new_password=NEW_PASSWORD)
    messages.add(caught.value.message)

    superseded = await _request_reset(service, notifier)
    await _request_reset(service, notifier)
    with pytest.raises(AuthenticationError) as caught:
        await service.reset_password(token=superseded, new_password=NEW_PASSWORD)
    messages.add(caught.value.message)

    expiring = _issued_token(notifier)
    db.password_reset_tokens[-1].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(AuthenticationError) as caught:
        await service.reset_password(token=expiring, new_password=NEW_PASSWORD)
    messages.add(caught.value.message)

    # Pinned to the literal, not to the constant: asserting only that the three
    # agree passes just as happily when all three name the reason.
    assert messages == {"Password reset link is invalid or expired."}


async def test_reset_revokes_every_existing_session(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # The point of resetting a compromised password is that whoever knew the
    # old one is locked out; live refresh tokens would make that cosmetic.
    await _register(service)
    await service.login(email=EMAIL, password=PASSWORD)
    await service.login(email=EMAIL, password=PASSWORD)
    assert len([t for t in db.refresh_tokens if t.revoked_at is None]) == 2

    token = await _request_reset(service, notifier)
    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    assert [t for t in db.refresh_tokens if t.revoked_at is None] == []


async def test_reset_revokes_sessions_across_separate_logins(
    service: AuthService, db: FakeDatabase, notifier: FakePasswordResetNotifier
) -> None:
    # Two logins are two families. Revoking one family would leave the other
    # alive, so this is what distinguishes revoke_all_for_user from
    # revoke_family.
    await _register(service)
    await service.login(email=EMAIL, password=PASSWORD)
    await service.login(email=EMAIL, password=PASSWORD)
    assert len({t.family_id for t in db.refresh_tokens}) == 2

    token = await _request_reset(service, notifier)
    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    assert all(t.revoked_at is not None for t in db.refresh_tokens)


async def test_an_old_refresh_token_stops_working_after_a_reset(
    service: AuthService, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    session = await service.login(email=EMAIL, password=PASSWORD)
    token = await _request_reset(service, notifier)

    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    with pytest.raises(AuthenticationError):
        await service.refresh(session.refresh_token)


async def test_a_failed_reset_commits_nothing(
    service: AuthService, factory: FakeUnitOfWorkFactory, db: FakeDatabase
) -> None:
    await _register(service)
    before = db.users[0].password_hash
    commits = sum(uow.commit_calls for uow in factory.created)

    with pytest.raises(AuthenticationError):
        await service.reset_password(token="never issued", new_password=NEW_PASSWORD)

    assert sum(uow.commit_calls for uow in factory.created) == commits
    assert db.users[0].password_hash == before


async def test_reset_uses_one_transaction(
    service: AuthService, factory: FakeUnitOfWorkFactory, notifier: FakePasswordResetNotifier
) -> None:
    await _register(service)
    token = await _request_reset(service, notifier)
    opened = len(factory.created)

    await service.reset_password(token=token, new_password=NEW_PASSWORD)

    # One unit of work, committed once: the hash change, the consumption, and
    # the revocations must land together or not at all.
    assert len(factory.created) == opened + 1
    assert factory.created[-1].commit_calls == 1
