# Orqent — Authentication Hardening

A four-milestone follow-up to the ten-phase backend. It changes no execution,
queue, worker, scheduler, RAG, or tool architecture; its whole surface is
`app.schemas.auth`, `app.services.auth_service`, `app.api.v1.routes.auth`, and
the frontend's auth feature.

| Milestone | Scope | Status |
|---|---|---|
| **AH1** | Generic login-failure semantics + password-policy audit | ✅ |
| **AH2** | Forgot/reset password lifecycle | ✅ |
| **AH3** | HttpOnly refresh-token persistence | ✅ |
| **AH4** | Rate limiting + final auth/security acceptance | ✅ |

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


---

## AH2 — forgot/reset password

### Endpoints

| Endpoint | Request | Success | Failure |
|---|---|---|---|
| `POST /api/v1/auth/forgot-password` | `{email}` | `200` — *"If an account exists for that email, a password reset link has been sent."* | none reachable by a client |
| `POST /api/v1/auth/reset-password` | `{token, new_password}` | `200` — *"Your password has been reset. Please sign in."* | `401` — *"Password reset link is invalid or expired."* |

`401` rather than `404` for a bad link: an invalid reset token is a rejected
credential, which is how `/auth/refresh` already treats a token it will not
accept. A `404` would also be a statement about existence, which is the one
thing this flow must never make.

Reset deliberately returns **no session**. Handing back a token pair would give
a session to whoever holds the link rather than to whoever knows the new
password, and would immediately undo the revocation the reset just performed.

### Token lifecycle

* **Class.** A third credential type, minted in
  `infrastructure/security/password_reset_token.py` — not a JWT, not a webhook
  token. Reusing an access or refresh JWT would let a leaked reset link be
  replayed against `/auth/refresh`.
* **Generation.** `secrets.token_urlsafe(32)` — 256 bits from the OS CSPRNG,
  rendered base64url as 43 characters needing no escaping in a query string.
* **At rest.** Only `sha256(token)` is stored, in `password_reset_tokens.
  token_hash`. The raw token exists in memory for one request and is never
  persisted, logged, or returned. The table has **no column** a plaintext token
  could be written to, which a metadata test asserts directly.
* **TTL.** `APP_PASSWORD_RESET_TOKEN_TTL_SECONDS`, default **1800 (30 min)** —
  far shorter than either JWT, because the link travels through email, a
  channel the platform neither controls nor can revoke.
* **Single use.** Redemption takes `SELECT … FOR UPDATE` on the digest row, the
  same idiom refresh rotation uses. Checking `consumed_at` and updating it
  later would pass every sequential test and still let two simultaneous
  requests both succeed: under MySQL's default REPEATABLE READ the second would
  read its own snapshot. Proved by a real two-transaction concurrency test, and
  that test fails when `for_update` is removed.
* **Supersession.** Issuing a link consumes every outstanding link for that
  user, so only the newest works. A successful reset does the same, so a second
  outstanding link cannot change the password again.
* **Session revocation.** A reset calls `revoke_all_for_user` — every family,
  not just one, since the point of resetting a possibly-compromised password is
  that the other party is locked out. Password change, grant consumption,
  supersession, and revocation all commit in **one transaction**.

### Enumeration behaviour

`forgot-password` returns the same status, body, and headers for an address
with an account, one without, and one belonging to a **disabled** account —
verified end-to-end over HTTP against real MySQL. A disabled account is treated
exactly as a missing one; letting a deactivated user reset their way back in
would make deactivation advisory.

Delivery failures are absorbed rather than surfaced. Reporting one would
confirm the address was worth delivering to, rebuilding the oracle by accident.

**No claim of timing equivalence is made.** An unknown address short-circuits
before the insert and before delivery, so it is measurably cheaper than a known
one. Unlike login — where a dummy Argon2 verification equalises the expensive
step — nothing here has been equalised or measured. Rate limiting (AH4) is the
mitigation that matters for this endpoint.

### Password policy

`enforce_password_complexity` in `app/schemas/auth.py` is the **single
definition**, called by both `RegisterRequest` and `ResetPasswordRequest`. A
reset flow with a laxer rule would quietly become the way to install a weak
password; a test compares the two endpoints' verdicts on the same candidates
rather than restating the rule.

### Delivery — what actually happens today

The `PasswordResetNotifier` port is provider-neutral and takes a **finished
URL**, never the token or the user. There is exactly one implementation,
`UnconfiguredPasswordResetNotifier`, and **it delivers nothing**: the link is
generated and discarded. In a deployment with no provider configured, a user
can request a reset and will never receive one. Selecting a provider remains
deployment work.

A "log the reset link so a developer can copy it" adapter was **rejected, not
merely omitted**. The reset URL is a bearer credential that changes a password,
and application logs are shipped, aggregated, retained, and read by more people
than the mailbox would have been. An architecture test asserts the shipped
adapter reads neither of its arguments.

`APP_PASSWORD_RESET_URL_BASE` names the frontend route the link points at; the
token is appended as the only query parameter. Unset means resets cannot be
delivered — and the endpoint still answers identically.

### Frontend

`/forgot-password` submits an email and always shows the same generic
confirmation; there is no "no account found" branch. `/reset-password?token=…`
reads the token from the query string into component state only — never
`localStorage`, `sessionStorage`, a cookie, or a log — and on success replaces
the URL so the spent token leaves browser history. No credentials are stored;
the flow ends at sign-in.

### AH4 attachment point

`POST /api/v1/auth/forgot-password` is a **mandatory** limiter attachment point,
keyed by client IP and by submitted email — it mints credentials and sends mail
on an unauthenticated request. `/reset-password` needs one too, to bound
guessing against the digest index. Nothing has been added for either.


---

## AH3 — HttpOnly refresh-token persistence

### What changed

The refresh token moved out of `localStorage` and into a cookie the browser's
JavaScript cannot read. The access token stays **in memory only**, unchanged.

| | Before | After |
|---|---|---|
| Access token | memory | memory |
| Refresh token | `localStorage['orqent.refresh']` | HttpOnly cookie |
| `POST /auth/login` body | `access_token`, `refresh_token` | `access_token` only |
| `POST /auth/refresh` | body `{refresh_token}` | **no body** — reads the cookie |
| `POST /auth/logout` | body `{refresh_token}` | **no body** — reads the cookie |
| Session restore | read storage, then refresh | refresh; the cookie is attached automatically |

`TokenPairResponse` is gone, replaced by `AccessTokenResponse`, which has **no
`refresh_token` field to populate** — a stronger guarantee than remembering not
to populate one. `RefreshRequest` is gone entirely: a client cannot present a
refresh token it merely possesses, so one captured from a log or pasted into a
console is useless without the browser holding the cookie.

**What this does and does not buy.** HttpOnly is not an XSS fix. A script that
compromises the page can still make requests while the tab is open. What it
cannot do any more is *exfiltrate* a thirty-day credential to replay elsewhere
at leisure. That is the whole gain, and it is worth stating narrowly.

### Cookie attributes

| Attribute | Value | Why |
|---|---|---|
| Name | `orqent_refresh` | Not a secret; the only part anyone is meant to see. |
| `HttpOnly` | always `true` | The reason the milestone exists. Asserted from the AST, not by string search. |
| `Secure` | `false` locally, **required** in production | A Secure cookie is never sent over the plain HTTP local dev runs on. Startup **refuses** `Secure=false` when `APP_ENVIRONMENT=production`. |
| `SameSite` | `lax` | See below. `none` is accepted but refused without `Secure`. |
| `Path` | `/api/v1/auth` | The narrowest path covering login, refresh and logout. The browser never attaches this credential to a workflow, run, or document request. |
| `Domain` | unset | Host-only, which is what localhost needs and what a single-host deployment wants. Setting it widens who receives the cookie. |
| `Max-Age` | `refresh_token_ttl_seconds` (30 days) | Two expressions of one deadline. The server stays authoritative: a cookie outliving its row is still refused. |

### Why SameSite=Lax

Orqent's frontend and API are **same-site in every supported deployment**:

* **Local, default** — Vite proxies `/api` to the backend, so the browser sees
  one origin.
* **Local, direct** — `VITE_API_BASE_URL=http://localhost:8000` is cross-*origin*
  but same-*site*: SameSite is a site-level rule that ignores the port.
* **Production** — an `app.` / `api.` split under one registrable domain is
  same-site.

Lax therefore sends the cookie on the app's own requests while blocking the
cross-site POSTs CSRF depends on. `SameSite=None` would only be needed for
genuinely cross-site origins (different registrable domains), which is not this
model, and it was not chosen casually.

### CSRF analysis

The cookie authenticates exactly **two** endpoints, `/auth/refresh` and
`/auth/logout`. Every other endpoint uses a bearer `Authorization` header, which
a cross-site attacker cannot set.

* `SameSite=Lax` does not send the cookie on cross-site POSTs, so a hostile page
  cannot invoke either endpoint at all.
* Even if one were invoked, the damage is bounded: logout is a nuisance, and
  refresh rotates the cookie while the attacker cannot read the response (CORS
  forbids it) and so never obtains the access token. Neither is an
  exfiltration path.
* A **same-site** attacker — a compromised sibling subdomain — would defeat this,
  as it would defeat most cookie schemes. Out of scope, and stated rather than
  implied.

**Conclusion: SameSite is sufficient here and no CSRF token subsystem was
built.** Inventing a half-correct one would add moving parts without closing a
reachable attack. This conclusion is tied to the same-site assumption above: a
future genuinely cross-site deployment forcing `SameSite=None` **must** revisit
it.

### CORS

`allow_credentials=True` is required for the browser to attach the cookie
cross-origin, so a wildcard origin is never valid. Startup now **refuses**
`'*'` in `cors_origins` — not because browsers would honour the combination
(they refuse it), but so the configuration cannot exist and tempt somebody into
"fixing" the resulting breakage by reflecting arbitrary `Origin` headers.

The default local setup needs no CORS at all: the Vite proxy makes the browser
see one origin. Explicit origins are only needed when pointing the frontend
straight at port 8000.

### Session restoration, and a defect the browser caught

On load the app calls `/auth/refresh`; the browser attaches the cookie; a new
access token comes back. Failure settles cleanly into a logged-out state with no
retry loop.

Restoration goes through `refreshSession`, which **deduplicates concurrent
callers** — and this is load-bearing rather than tidy. The first implementation
called the API directly, bypassing that dedup, and live browser testing found
the consequence: React StrictMode invokes the mount effect twice, so two
refreshes carried the *same* cookie, the second was a replay, reuse detection
revoked the family, and **every reload logged the user out**. It passed every
unit test and every type check. The fix routes restoration through the shared
in-flight promise; an architecture guard now pins it.

Worth recording why it appeared only now: before AH3, `setRefreshToken` wrote to
`localStorage` synchronously, so a second caller read the *new* token. Moving
storage out of JavaScript removed that synchronous handoff and changed the
concurrency properties of a code path that looked untouched.

### Invalid-cookie handling

A refresh that is rejected — missing, expired, revoked, replayed — clears the
cookie. Leaving one the server will never accept again means the app retries
with it on every page load, turning one dead session into a stream of 401s.
This is **client-side tidy-up only**: revocation, including reuse detection's
family sweep, already happened server-side and the row remains authoritative.
Missing and dead cookies produce byte-identical responses.

### Password-reset interaction

AH2's `revoke_all_for_user` still reaches a session held in a cookie. The
browser keeps the cookie — a server cannot reach into a browser it is not
currently talking to — so the honest property is that the cookie is still
*present* and no longer *works*: the next refresh is rejected and the cookie is
cleared then. Server state is authoritative.

### Rotation and reuse

Unchanged by the transport. Login issues cookie A; refresh rotates it to B and
then C; replaying A is caught as reuse and revokes the whole family, so even the
legitimate successor stops working. The frontend never sees any of these values.
Verified end-to-end over HTTP against real MySQL.

### AH4 attachment points

`/auth/login`, `/auth/register`, `/auth/forgot-password`, `/auth/reset-password`
and now `/auth/refresh` — the last because it is unauthenticated in the bearer
sense and reachable with only a cookie. Nothing has been added.


---

## AH4 — rate limiting and final acceptance

### Protected endpoints and limits

| Endpoint | Default | Keys | Why |
|---|---|---|---|
| `POST /auth/login` | `8/60` | IP + email digest | The email key bounds credential stuffing spread across addresses. |
| `POST /auth/register` | `5/60` | IP | **Not** keyed on email: throttling differently per address would let someone probe which are taken. |
| `POST /auth/forgot-password` | `5/3600` | IP + email digest | Sends mail, so an unbounded one is both an enumeration tool and a way to deliver a stranger's inbox a hundred messages. |
| `POST /auth/reset-password` | `8/60` | IP + token digest | Bounds guessing against the digest index. |
| `POST /auth/refresh` | `60/60` | IP | Generous: a browser refreshes on every reload, and a tight limit here looks like a broken session. |
| `POST /hooks/{token}` | `120/60` | IP + token digest | An order of magnitude higher — a busy integration legitimately delivers continuously. |

`POST /auth/logout` is deliberately **not** limited: a throttled logout leaves
somebody signed in against their wishes, which is the wrong failure. Everything
else — workflows, runs, documents — is untouched; the blast radius is the
unauthenticated, abuse-sensitive surface only.

All values are settings (`APP_RATE_LIMIT_*`), so a deployment tunes them
without a code change. `APP_RATE_LIMIT_ENABLED=false` turns the whole thing off.

### Algorithm, storage, and the guarantee — stated plainly

**Sliding window log**, in **this process's memory**.

* *Sliding, not fixed*: a fixed window lets a caller spend the whole allowance
  at 0:59 and the whole of the next at 1:01, sustaining twice the intended rate
  precisely when someone is trying. It also yields an honest `Retry-After` — the
  moment the oldest hit ages out, computed rather than guessed.
* *Clock*: `time.monotonic`, not wall-clock. A limiter on wall-clock time would
  grant a free allowance whenever an NTP correction stepped the clock backwards.
* *Refusals are not recorded.* A rejected request that still counted would let a
  caller hold themselves over the limit indefinitely by retrying — turning a
  one-minute limit into an indefinite ban driven by the client's own retry loop.

**The guarantee is per process.** Orqent runs one API container with one uvicorn
worker, so today that is the whole deployment and the limit is the limit. Run
two replicas and each enforces its own counter, so the effective ceiling
doubles. This is written here, in the limiter's module docstring, and in the
settings, because "rate limited" must not be read as "distributed rate limited".

Redis was **not** added: there is none in the deployment, and introducing one to
gain cross-replica accuracy for a single-replica system buys nothing today at
the cost of a new operational dependency. MySQL was rejected too — it would put
a write on the hot path of every login, plus a migration and a sweep job, to
solve a problem this deployment does not have. The limiter is one small class
behind one call, so swapping the storage later is a contained change.

**Failure mode:** there is no external store, so there is nothing to be
unavailable. The limiter cannot fail independently of the process it lives in.
A distributed backend would need that decision made explicitly; this one does
not have it to make.

**Concurrency:** `check` performs its read, decision and write with no `await`
between them, so under asyncio the sequence cannot interleave and concurrent
requests in this process are counted exactly. Proven, not asserted: ten
simultaneous requests against a limit of four yield exactly four `200`s. It is
**not** safe across OS threads or processes and does not claim to be.

**Memory:** keys are swept lazily; any key whose newest hit is older than the
longest window in use is dropped. Without that the map grows one entry per
distinct address forever, which an attacker can drive deliberately by rotating
source addresses — an unbounded-growth bug is a denial-of-service vector, not
untidiness. A key still inside its own window is never swept.

### Client identity and proxies

**Forwarded headers are ignored by default** (`trusted_proxy_hops = 0`). Any
client can send `X-Forwarded-For`; honouring it without knowing how many proxies
the app actually sits behind would let an attacker mint a fresh identity per
request and make every limit here decorative. The peer address is the only value
the application can verify.

With `trusted_proxy_hops = N` the Nth entry **from the right** is used: the
rightmost N were appended by proxies we control, and the one before them is what
the outermost trusted proxy saw. Counting from the right is essential — a client
can prepend as many fake entries as it likes, and only the right-hand end is
trustworthy. Setting N larger than the real number of proxies reintroduces
exactly the forgery it prevents.

*Local and direct deployment*: no proxy, so the peer address is the client.
*Behind a reverse proxy*: set `APP_TRUSTED_PROXY_HOPS` to the real hop count.

### The 429 contract

`429` with the standard envelope, code `rate_limit_exceeded`, message *"Too many
requests. Please try again later."* — and nothing else. No counter, no limit, no
key, no address, no indication whether the account or token exists. A
`Retry-After` header carries the computed wait.

### Privacy

Emails and tokens are SHA-256 digested before becoming part of a key, using the
same helper that stores refresh and webhook tokens. Limiter state and any log
line built from it hold pseudonyms rather than credentials or personal data —
abuse prevention must not be bought with a searchable record of every address
that touched the API. Refusals log the category and correlation id only.

### Enumeration safety, re-verified

Rate limiting is the most plausible way to undo AH1 and AH2, so it is tested
directly: a known and an unknown address hit the wall at the *same request* on
both `/auth/login` and `/auth/forgot-password`, because the limiter never looks
up whether the subject exists. Mutating the code to limit only existing accounts
fails the suite.

### Known limitations

* Per-process enforcement, as above.
* No distributed or persistent state: a restart clears every counter, so an
  attacker who can trigger restarts gets a fresh allowance.
* `trusted_proxy_hops` is a hop count, not an allowlist of proxy addresses. A
  hostile host *inside* the trusted chain could still forge the client address.

### A note on running the integration suite

The suite intermittently failed one test (`test_rag_runtime::
test_the_runs_organization_reaches_the_invoked_node`, a run ending `FAILED`
instead of `COMPLETED`) while the docker-compose **`worker` and `dispatcher`
containers were running against the same MySQL**. Those containers poll the
queue, so they occasionally claim a task a test had just enqueued and execute it
in a process with none of the test's doubles.

Confirmed by experiment rather than assumed: three consecutive full runs with
the containers stopped gave **653/653**, against roughly one failure per run
with them up. Stop `worker` and `dispatcher` before running the integration
suite, or point the tests at a separate database.
