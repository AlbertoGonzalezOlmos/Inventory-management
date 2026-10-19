# Task 2 proposal (qwen3.8-max) — Auto QR badge at account creation

Concrete implementation approach for Task 2 of `docs/plans/COORDINATOR.md`
("`register()` / `create_member()` generate a badge, returned once (printable
SVG); seeds excluded; tests"). Proposal only — no code changed by this
document. Derived against the current working tree (Task 1 landed).

**Verified ground truth** (read this pass, not assumed):

- `User(...)` is constructed in exactly **four** places: `app/main.py:47`
  (`seed_default_admin`), `app/main.py:78` (`_ensure_dev_admin`),
  `app/routers/auth.py:63` (`register`), `app/routers/members.py:64`
  (`create_member`). Seeds live in `app/main.py`, **not** `app/seed.py`
  (which only seeds example items + backfills embeddings).
- `users.qr_badge_hash` is written in exactly four places: the self-service
  rotate/revoke (`auth.py:132/143`) and the staff rotate/revoke
  (`members.py:202/216`). There is **no** model default, event hook, or
  startup backfill touching it.
- `register()`: builds `User`, `session.add`, `commit` inside
  try/`IntegrityError` → rollback + 409, `session.refresh(user)`, returns
  `_issue_token(session, user)` → `AuthOut`, status 201.
- `create_member()`: same shape, `must_change_password=True`, returns
  `UserOut`, status 201.
- `QrBadgeOut {payload, svg}` is the established one-time badge response
  (both rotate endpoints); `badge_svg()` is pure (vendored Nayuki encoder,
  CSP-safe markup, already validated by `test_badge_svg_is_well_formed` and
  the `v-html` sink test).
- `UserOut.has_qr_badge` is a derived property (`qr_badge_hash is not None`)
  — correct for free once the hash is set before the response is built.
- `tests/test_metadata_and_ops.py` does **not** snapshot OpenAPI
  schemas/routes (version/README/docs-kill-switch/hygiene tests) — no
  snapshot refresh needed for added response schemas.
- `tests/conftest.py::create_user` asserts `r.json()["must_change_password"]
  is True` on the create-member response — an additive response field keeps
  it green.

---

## 1. Where and how the badge is generated

**One new helper, two one-line call sites.** Badge generation must be
identical in both endpoints and must never drift from the rotate endpoints,
so factor the "generate payload → store hash" pair (the part that must happen
*before* the commit) into `app/qrbadge.py`-adjacent logic — concretely, a
tiny helper next to the existing badge code in `app/routers/auth.py` is
wrong (members.py would import from a sibling router); put it in
**`app/qrbadge.py`**, which already owns the credential format and has no
router/model dependencies beyond a new `token_hash` import:

```python
# app/qrbadge.py  (addition)
def issue_badge(user) -> str:
    """Generate a fresh badge for ``user`` and store its hash (in memory).

    Returns the raw payload — the caller is responsible for committing and
    for showing the payload exactly once. Mirrors the rotate endpoints:
    only the SHA-256 hash ever reaches the database.
    """
    from app.security import token_hash  # local import: qrbadge stays leaf-level
    payload = new_badge_payload()
    user.qr_badge_hash = token_hash(payload)
    return payload
```

(Alternative: keep it inline in both routers — two duplications of a
security-critical pair. The helper is worth the 8 lines; the rotate endpoints
may adopt it later but are **not** touched in this task.)

**`register()`** (`app/routers/auth.py`) — set the hash on the `User`
*before* `session.add`, so badge and account commit **atomically in the same
row insert**:

```python
    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
        role="member",
    )
    badge_payload = issue_badge(user)          # ← hash set pre-commit
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Email already registered")
    session.refresh(user)
    auth = _issue_token(session, user)
    return RegisterOut(
        token=auth.token,
        user=auth.user,
        qr_badge=QrBadgeOut(payload=badge_payload,
                            svg=badge_svg(badge_payload)),
    )
```

**`create_member()`** (`app/routers/members.py`) — identical placement
(before `session.add`, inside the existing try/IntegrityError), and the
response becomes `MemberCreatedOut.model_validate(user)` plus the badge:

```python
    badge_payload = issue_badge(user)          # ← hash set pre-commit
    session.add(user)
    try:
        session.commit()
    except IntegrityError: ...                 # unchanged 409 path
    session.refresh(user)
    return MemberCreatedOut(
        **UserOut.model_validate(user).model_dump(),
        qr_badge=QrBadgeOut(payload=badge_payload,
                            svg=badge_svg(badge_payload)),
    )
```

Ordering rules (both endpoints, mirroring the existing rotate endpoints):

1. **payload + hash before `add`/`commit`** — the hash lands in the same
   INSERT as the account; there is no window where the account exists
   without its badge and no partial state to clean up;
2. **`badge_svg()` after the commit** (in the response expression) — SVG
   rendering is pure CPU work over 49 chars; doing it post-commit matches
   `create_my_badge` exactly. If it could ever raise, the failure mode is a
   500 *after* a successful creation, i.e. "account exists, badge hash
   stored, raw payload lost" — recovery is the existing rotate flow
   (`POST /api/auth/qr-badge`), the same recovery the rotate endpoints
   already have. No compensation logic warranted.

Endpoint decorators change only in their response models:
`@router.post("/register", response_model=RegisterOut, status_code=201)` and
`@router.post("", response_model=MemberCreatedOut, status_code=201)`.
No other behaviour of either endpoint changes.

## 2. Response shape — badge exactly once, no leak into ordinary responses

**Reuse `QrBadgeOut` verbatim** (`{payload, svg}`): it is already the
"shown exactly once" contract of the rotate endpoints, its docstring already
says the payload is returned once and only its hash persists, and the SPA
already knows how to render it (Task 5 reuses the `memberBadge` print modal).
Inventing a second badge-response type would only create drift.

**Subclasses, not shared-schema mutations** (the kimi/glm-converged choice
from planning; adopted):

```python
# app/schemas.py  (additions, next to QrBadgeOut)
class RegisterOut(AuthOut):
    """Registration response: the session PLUS the account's one-time QR
    badge. Deliberately a subclass of AuthOut rather than a new optional
    field on it: AuthOut is also the *login* (password AND qr-login)
    response, and login responses must never carry badge material — a
    badge is shown exactly once, at creation or rotation."""

    qr_badge: QrBadgeOut


class MemberCreatedOut(UserOut):
    """Staff-created account: UserOut plus the one-time badge for printing
    the membership card at the counter."""

    qr_badge: QrBadgeOut
```

- `qr_badge` is **required** (not `| None`): both endpoints now always issue
  one, and a required field makes "creation without badge" unrepresentable
  in the response contract — a regression would fail schema validation, not
  slip through as `null`.
- Field name `qr_badge` (not `badge`): consistent with the
  `/qr-badge` route names and `has_qr_badge`.
- **`AuthOut` and `UserOut` are untouched**, which is the whole leak
  guarantee: `POST /auth/login`, `POST /auth/qr-login`, `GET /auth/me`,
  `GET /members`, `PATCH /members/{id}`, and the badge rotate/revoke
  responses keep their exact current shapes. The only responses that ever
  carry `payload`/`svg` are the two creation responses and the two existing
  rotate responses — pin this with a test-side inventory (§5, T4).
- Additive-only risk: consumers that field-wise parse these two responses
  (`conftest.create_user`, `scripts/smoke_test.py`, `scripts/ui_check.py`,
  bridge untouched) ignore unknown keys; `create_user`'s
  `must_change_password` assertion still holds.

## 3. Seeds exclusion — structural, not by convention

The guarantee has three legs:

1. **Structural**: badge generation lives *only* in the two endpoint
   functions (via `issue_badge`). `seed_default_admin` and
   `_ensure_dev_admin` construct `models.User(...)` directly and are not
   modified in this task; `User.qr_badge_hash` keeps its `default=None`;
   there are no SQLAlchemy event hooks, no startup backfill, and no
   migration touching the column (verified inventory above: 4 construction
   sites, 4 hash-write sites). A seeded badge is therefore impossible
   without editing a seed function — which the tests below would catch.
2. **Deliberate** (documented in both seed docstrings with a one-line
   comment): a badge payload can only be shown once, at generation time,
   and seeds have no human to show it to — a generated hash nobody can
   print is dead weight, and logging the payload would put a bearer
   credential in stdout/journald. Seeded admins get badges the supported
   way: staff rotation (`POST /api/members/{id}/qr-badge`, admin actor for
   admin targets) or, for the dev admin, nothing (it is a debug
   credential that `run.sh` already refuses to expose non-loopback).
3. **Test-pinned** in both boot modes (§5, T8/T9): the secure-default suite
   asserts the seeded admin's hash is NULL in the DB and
   `has_qr_badge=False` via `/me`; the dev-admin subprocess test extends
   `_BOOT_SNIPPET`'s `accounts` tuples with `u["has_qr_badge"]` (already in
   `UserOut`) and asserts `False` for **both** the dev admin and the legacy
   admin.

**No backfill for existing accounts** (all three peer plans agreed, the
coordinator adopted it): accounts created before this change keep
`has_qr_badge=False` and the existing generate/rotate buttons; a bulk
backfill is impossible by design because the payload could never be shown.
One sentence each in the two seed docstrings + the Task-6 docs pass.

## 4. Edge cases

- **IntegrityError race (concurrent duplicate email).** The badge hash is a
  field on the uncommitted `User` row: when the race loser rolls back, *both*
  the account and its hash vanish; the generated payload was never persisted,
  never returned, and is garbage-collected — nothing lost, nothing leaked,
  the 409 contract unchanged. "The badge must not be lost" is satisfied in
  the only sense that matters: **iff the account commits, the returned
  payload works** (hash and payload are two views of one atomic write) —
  pinned by T1/T2/T6 (response badge logs in) rather than by a race test
  (a deterministic concurrent-register race is not reproducible in this
  suite; the atomicity argument is structural).
- **Client disconnects before reading the response** (payload lost after
  commit): account exists, `has_qr_badge=True`, raw payload unrecoverable —
  recovery is rotate (`POST /api/auth/qr-badge` replaces and re-shows). Same
  failure class as the existing rotate endpoints; documented in the
  endpoint docstring.
- **`has_qr_badge` correctness**: derived property, hash set pre-commit;
  `session.refresh(user)` after commit re-reads the row, so both the
  `RegisterOut.user` and `MemberCreatedOut` copies carry `True`. Pinned by
  tests (T1/T6) including the members-listing view (T4).
- **Badge + `must_change_password` (staff-created accounts)**: unchanged
  semantics — `qr-login` with the creation badge issues a token, and the
  existing gate (`deps.get_current_user`) 403s everything except the exempt
  paths until the password is changed. `test_badge_login_does_not_bypass_the_password_change_gate`
  already pins the gate for rotated badges; T7 extends the pin to
  creation badges (no gate regression, no bypass).
- **Uniqueness**: `secrets.token_urlsafe(32)` — collisions are
  cryptographically irrelevant; no dedup check needed (and the column is
  non-unique by design: NULLs for badgeless accounts). T10 sanity-pins
  distinctness across two registrations.
- **Auto-badges are ordinary badges**: rotate kills the creation payload,
  revoke kills it, delete-member destroys it (existing
  `test_deleted_members_badge_dies_with_account` covers the last). T11 pins
  rotate/revoke on a *creation* badge so nothing about auto-issuance is
  special.
- **SVG safety**: unchanged pipeline (vendored encoder, `badge_svg`), already
  covered by `test_badge_svg_is_well_formed` + the `v-html` sink test; T1/T6
  re-assert shape on the creation responses.
- **Roles**: `create_member` issues badges uniformly for member/staff/admin
  targets. No privilege interaction: the *creation* actor is whoever may
  create that role at all (staff→member/staff, admin→admin), while later
  *management* of an admin's badge stays admin-only (`_get_manageable_member`,
  unchanged, already tested).

## 5. Tests

Primary file: **`tests/test_qr.py`** (badge semantics live there), with two
small additions elsewhere. All use the existing fixtures/helpers (`client`,
`admin_token`, `create_user`, `auth`, `unique_email`).

- **T1 `test_register_returns_a_one_time_badge`** — `POST /auth/register` →
  201; response has `qr_badge.payload` (starts `HCRM1:`,
  `parse_badge_payload` accepts it, 43-char token) and `qr_badge.svg`
  (well-formed per the existing helper assertions); `response.user.
  has_qr_badge is True`; `token` still present and working (register remains
  a login).
- **T2 `test_register_badge_logs_in`** — `POST /auth/qr-login` with the
  returned payload → 200, same `user.id`, token works on `/me`. (Proves
  hash-at-rest == payload-shown: the atomicity pin.)
- **T3 `test_creation_badge_only_hash_at_rest`** — direct DB check (fresh
  `Session(engine)`, mirroring `test_only_the_hash_is_persisted`):
  `users.qr_badge_hash == token_hash(payload)` for both a registered and a
  staff-created account; the raw payload string appears nowhere in the row.
- **T4 `test_no_badge_material_outside_creation_and_rotation`** — the leak
  inventory: for a user created via register, assert the string `"qr_badge"`
  and the payload are absent from the JSON of: `POST /auth/login`
  (password), `POST /auth/qr-login`, `GET /auth/me`, `GET /members` (admin
  view — entry has `has_qr_badge: true`, no payload/svg),
  `PATCH /members/{id}`, `POST /auth/qr-badge` **revoke** (204). (Rotate
  responses legitimately carry a *new* payload — assert it differs from the
  creation one.) Also assert `login`'s response keys are exactly
  `{token, user}` — pins that `AuthOut` did not grow the field.
- **T5 `test_register_conflict_leaves_no_badge_trace`** — duplicate email →
  409; DB: exactly one user row for that email, its `qr_badge_hash` is the
  *first* registration's (unchanged); no orphan row, and the second
  response carries no payload.
- **T6 `test_create_member_returns_a_working_badge`** — staff creates a
  member → 201; `qr_badge` present and well-formed; `has_qr_badge: true` in
  the response and in `GET /members`; the badge logs in via `qr-login`;
  works for admin-created staff and admin-created admin accounts too
  (parametrise role).
- **T7 `test_creation_badge_respects_the_password_gate`** — staff-created
  (flagged) account: `qr-login` with the creation badge → 200 + token, but
  `/api/items` etc. → 403 until change-password; after the change the same
  badge works fully. (Extends the existing rotated-badge gate test to the
  creation path.)
- **T8 `test_seeded_admin_has_no_badge`** (main suite, `HCRM_DEV_ADMIN=0`) —
  `admin_token` fixture → `/me` shows `has_qr_badge: false`; direct DB:
  `qr_badge_hash IS NULL` for `admin@shop.local`.
- **T9 dev-admin boot report** (`tests/test_dev_admin.py`) — extend
  `_BOOT_SNIPPET`'s `accounts` comprehension to
  `(u["email"], u["role"], u["must_change_password"], u["has_qr_badge"])`
  and assert `has_qr_badge is False` for **both** seeded accounts in
  `test_dev_admin_mode_on`; adjust the tuple unpacking in the existing
  assertions (`emails = [a[0] for a in …]` stays valid).
- **T10 `test_creation_badges_are_unique`** — two registrations → distinct
  payloads and distinct DB hashes.
- **T11 `test_creation_badge_rotates_and_revokes`** — register → rotate via
  `POST /auth/qr-badge`: creation payload now 401 on `qr-login`, new payload
  works; revoke → 401. (Auto-issued badges are ordinary badges.)
- **Existing suites stay green unchanged**: `test_auth.py` (field-wise
  assertions), `test_qr.py` lifecycle tests (rotate/revoke semantics
  untouched), members tests, `conftest.create_user` (additive field),
  `test_metadata_and_ops.py` (no OpenAPI schema snapshot — verified).
  `tests/test_migrations.py` untouched: **no** schema change at all in this
  task (the `users.qr_badge_hash` column and its index already exist and are
  already in `COLUMN_MIGRATIONS`).

## 6. Files touched (review checklist)

| File | Change |
|---|---|
| `app/qrbadge.py` | + `issue_badge(user) -> str` (payload gen + hash-set, leaf-level, local `token_hash` import) |
| `app/schemas.py` | + `RegisterOut(AuthOut)`, `MemberCreatedOut(UserOut)` — both with required `qr_badge: QrBadgeOut`; `AuthOut`/`UserOut`/`QrBadgeOut` untouched |
| `app/routers/auth.py` | `register()`: `issue_badge` pre-add; `response_model=RegisterOut`; docstring (one-time payload; rotate = recovery) |
| `app/routers/members.py` | `create_member()`: `issue_badge` pre-add; `response_model=MemberCreatedOut`; docstring (print-at-counter) |
| `app/main.py` | one-line comments in `seed_default_admin` + `_ensure_dev_admin`: *deliberately no badge* (payload can only be shown once; seeds have no viewer; logging it would leak a credential) — comments only, no code change |
| `tests/test_qr.py` | T1–T8, T10, T11 |
| `tests/test_dev_admin.py` | T9 (snippet tuple + assertion) |
| *(untouched)* | `app/seed.py`, `app/models.py`, migration tuples, rotate/revoke endpoints, `app/routers/cart.py` |

## 7. Risks & open questions

1. **Registration responses now carry a printable bearer secret** (once).
   Same exposure class as the existing rotate flow; mitigations: never
   logged (no logger call near the payload — review checklist item), TLS/
   loopback deployment assumption unchanged, and Task 5 adds the
   print/acknowledge step ("shown only once") to the register UI. Accepted.
2. **Field naming** `qr_badge` vs glm's `badge`: `qr_badge` chosen for
   consistency with route names and `has_qr_badge`. Trivial to flip if the
   coordinator prefers.
3. **`issue_badge` placement** in `app/qrbadge.py` introduces a (local,
   function-level) import of `app.security.token_hash`; the alternative
   (module-level import) is also acyclic — `security.py` imports only
   `app.models`. Local import chosen to keep `qrbadge.py` dependency-free at
   module level (it is currently imported by both routers); reviewer taste.
4. **Should the rotate endpoints adopt `issue_badge` too?** Out of scope
   here (do not touch tested security code in a feature task), but it is the
   obvious follow-up dedup — flag for Task 6's review pass.
5. **No rate/quota interaction**: registration already has no rate limit;
   auto-badges add none (256-bit payloads, `docs/opticon-m10.md` §9.4
   rationale unchanged).
6. **Docs**: `docs/opticon-m10.md` §9.2's issuing table gains an "automatic
   at account creation" row; README badge paragraph one sentence. Can ride
   with this PR (repo is docs-with-code) or defer to Task 6 — recommend
   riding, it is three lines.
