# Orqent — Authentication Hardening

A four-milestone follow-up to the ten-phase backend. It changes no execution,
queue, worker, scheduler, RAG, or tool architecture; its whole surface is
`app.schemas.auth`, `app.services.auth_service`, `app.api.v1.routes.auth`, and
the frontend's auth feature.

| Milestone | Scope | Status |
|---|---|---|
| **AH1** | Generic login-failure semantics + password-policy audit | ✅ |
| **AH2** | Forgot/reset password lifecycle | ⬜ |
| **AH3** | HttpOnly refresh-token persistence | ⬜ |
| **AH4** | Rate limiting + final auth/security acceptance | ⬜ |

---

## AH1 — login account enumeration

### What the audit found

Most of AH1's intent was **already implemented** before this milestone began.
Recording that honestly matters more than claiming the work: the milestone's
real contribution was to the tests, not the code.

Already present and left unchanged:

* One shared `_INVALID_CREDENTIALS` constant covering unknown email, wrong
  password, **and** disabled account. All three raise `AuthenticationError`,
  which the API layer renders as `401` / `authentication_error`.
* A real Argon2id `_DUMMY_PASSWORD_HASH` verified on the unknown-email path, so
  a missing account costs the same CPU as a real verification. Guarded by
  `test_dummy_hash_uses_current_parameters`, which fails if the library's cost
  defaults move and the constant does not.
* `is_active` checked *after* verification, so a disabled account is not cheaper
  than an enabled one.
* Registration policy of 8+ characters, a letter, a number, and a special
  character, enforced server-side in `RegisterRequest`.
* `LoginRequest` deliberately carries **no** minimum length — a `422` for a
  short password would answer a login attempt differently depending on the
  input's shape, and would lock out accounts predating a policy change.

### What AH1 changed

* `_INVALID_CREDENTIALS` now reads **"Either email or password is
  incorrect."** — the only production line this milestone touched.
* Tests were strengthened where they did not discriminate. The pre-existing
  `test_every_login_failure_reports_the_same_message` asserted only that the
  three failure messages *agree*; it passed just as happily when all three said
  "user not found". It now pins the literal.
* Two integration tests run the real service — real MySQL, real Argon2 — through
  the real app over HTTP and assert the unknown-email and wrong-password
  responses are **byte-equivalent** in body (less `correlation_id`) and in
  headers. The unit endpoint tests substitute the service and structurally
  cannot make that claim.

### Password policy

"Special character" is defined as **not alphanumeric and not whitespace**
(`not c.isalnum() and not c.isspace()`), evaluated over Unicode rather than an
ASCII allowlist. `! @ # $ % ^ & * _ - ?` are each pinned by test.

Length is bounded at 1024 characters. That ceiling is a **resource guard, not a
policy**: Argon2 hashes whatever it is given, so an unbounded password is an
invitation to burn CPU on a megabyte of input. Argon2 has no short input limit
of its own (unlike bcrypt's 72 bytes), so no password is silently truncated.

The frontend mirrors the rule for UX only — `passwordPolicy()` in `LoginPage`
checks the same four conditions with `\p{L}` / `\p{N}` — and the backend remains
authoritative. AH1 required no frontend change: the checklist already matched,
and the login form already renders the envelope's message via `messageOf`,
never a raw exception body.

### Timing

The dummy-hash path removes the obvious *unknown user → immediate return* versus
*known user → expensive verify* difference, and a test asserts the verification
actually happens. **No claim of general timing-attack resistance is made**:
the database lookup itself, connection-pool behaviour, and the rehash-on-login
branch all remain unequalised, and none of that has been measured.

### Where rate limiting will attach (AH4, not implemented)

`POST /api/v1/auth/login`, `/register`, and `/refresh` are the three endpoints
that need limiting, keyed by client IP **and** by submitted email. Nothing has
been added for it — no Redis, no middleware, no counters.
