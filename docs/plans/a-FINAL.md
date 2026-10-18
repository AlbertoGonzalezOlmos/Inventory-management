# Task A FINAL spec — HMAC-derived, re-viewable QR badges

Merged from `a-proposal-{glm,kimi,qwen}.md` + `a-review-{glm,kimi,qwen}.md`.

## Key loader (`app/security.py`)

- `BADGE_SECRET_SOURCE` accessor; loader resolves, in order:
  1. `HCRM_BADGE_SECRET` env — if set but invalid (short/empty) → **fail fast** at import.
  2. else `data/badge.key` — if absent, auto-generate (32 random bytes, urlsafe) and
     write with `0600` + `O_EXCL` (loser re-reads the winner's file).
  3. else `None` (directory unwritable) — issuance falls back to a **random legacy
     (generation-0) badge**; re-view returns 409 no-key; login still works.
- `badge_secret()` returns `str | None` + a `source` string for healthz.

## Derivation (`app/qrbadge.py`)

- `derive_badge_payload(user_id, generation, secret) -> str` — pure, frozen:
  `"HCRM1:" + base64url(HMAC-SHA256(secret, f"badge:{user_id}:{generation}")[:32])`.
  Ship a known-answer test (KAT).
- `new_badge_for(user, secret=None)` becomes derivation-aware: on **rotation**
  `generation++`, derive, `user.qr_badge_hash = token_hash(payload)`, return payload.
  On **no-key** it issues a random legacy payload and keeps generation 0.
- `rederive_and_check(user, secret) -> payload | None`: recompute the payload for
  `user.qr_badge_generation` and return it only if `token_hash(payload) ==
  user.qr_badge_hash` (a rotated key can never render a wrong badge).

## Model (`app/models.py`)

- `User.qr_badge_generation: int = Field(default=0)` →
  `COLUMN_MIGRATIONS` entry `("users", "qr_badge_generation",
  "INTEGER NOT NULL DEFAULT 0", ())`. **0 = legacy** (random, non-derived).

## Issuance (register / create_member / rotate)

- **flush-then-derive** in a single transaction: add the user, `session.flush()`
  to obtain the committed `id`, then `new_badge_for` (derive using the id) and
  commit — no badgeless-account window. Preserve the existing
  `IntegrityError → 409` duplicate-email handling.

## Re-view endpoints

- Shared `view_badge(session, user)` in `app/qrbadge.py` returning a domain
  result; the routers map it to status-convention envelopes (house style:
  a `status` discriminator, not a plain `detail` string):
  - no badge → 404 `{status:"none"}`;
  - generation 0 → **idempotent auto-migration**: `generation=1`, derive, store
    hash, return `{status:"migrated", …}` (the old printed badge dies);
  - no key → 409 `{status:"no_key"}`;
  - key mismatch (derived hash ≠ stored) → 409 `{status:"key_mismatch"}`;
  - ok → 200 `{status:"ok", …}`.
- `GET /api/auth/qr-badge` (self, repeatable) and staff
  `GET /api/members/{id}/qr-badge` (manage rule).
- `QrBadgeViewOut {payload, svg, generation, migrated}`.

## Untouched

`qr-login`, `qr-identify`, `_badge_from_scanned`, the bridge, and the SPA scan
gates — `qr_badge_hash` remains the sole login authority.

## Ops

- `scripts/backup_db.sh`: copy `data/badge.key` alongside the DB and print a
  loud "backup is now credential-bearing" warning.
- `app/main.py` healthz: expose `badge_key: true/false` + source.

## Tests

New `tests/test_badge_derivation.py` (~20 cases): KAT; determinism/distinctness;
key bootstrap (0600, O_EXCL, invalid-env fail-fast, no-key fallback); issuance
gen-1 + both login forms; re-view twice identical; rotation semantics;
wrong-key-never-authenticates (monkeypatched + subprocess different secret;
forged payload → 401); key-mismatch ⇒ login still works; legacy auto-migration
(idempotent, old payload dies); no-badge 404; no-key → register still 201
random + healthz false; email-change never touches the badge; staff-view
privilege matrix; gate 403; flush-race no-trace. Extend `test_qr.py`,
`test_migrations.py` (exact-set + legacy-badge-still-logs-in),
`test_backup_and_run.py` (keyfile case). Docs-truth minimum: §9.1/§9.4 gain the
derivation paragraph now.
