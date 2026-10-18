# Task A cross-review (glm) — of `a-proposal-kimi.md` and `a-proposal-qwen.md`

Reviewer context: my own proposal is `docs/plans/a-proposal-glm.md`. All
three implement the coordinator's adjudicated design verbatim (HMAC
derivation, hash authority untouched, flush-then-derive, legacy
auto-migrate, structured 409s, healthz, backup warning, graceful no-key
issuance, subprocess wrong-key test). The deltas are placement, defaults,
and five genuinely good catches.

## 1. Kimi

**Adopt:**
- **Key in `app/security.py`** (2-of-3 over my `qrbadge.py`): it is the
  credentials module and the home of every env-at-import knob; the badge
  domain imports it already.
- **`O_CREAT|O_EXCL`** for the keyfile (no creation race, right perms from
  birth — I said `os.open(..., 0o600)` without the EXCL discipline).
- **Generation never decrements on revoke** — re-issue bumps from the last
  value, so a revoked-then-reissued badge can never equal an older one
  (monotonicity pin I left implicit).
- `badge_key_source: "env"|"file"` in healthz (visibility beyond the boolean).
- The backup test extension (keyfile lands in the backup dir).

**Rejected:**
- Kimi's `badge_secret()` has no invalid-env story and no explicit
  no-key-at-issuance fallback (auto-generation "must boot" covers the
  common case but not an unwritable data dir). Both gaps are covered by
  the merged design below.

## 2. Qwen

**Adopt (five real catches):**
- **The path constant without importing `app.database`** — computing
  `<HCRM_DATA_DIR or repo/data>/badge.key` with a keep-in-sync comment
  avoids dragging `app.embeddings`/fastembed into every security consumer
  for one path. Beats both my lazy import and kimi's module-level one.
- **The KAT (known-answer test)**: the derivation formula is a
  forever-contract (any change orphans every stored hash); a frozen
  fixed-secret → exact-payload vector makes silent drift unmergeable. The
  single best addition of either proposal.
- **The RegisterOut argument for flush-over-two-phase**: `RegisterOut`
  *requires* `qr_badge`, so a phase-2 failure would 500 **after** creating
  the account — the decisive justification, sharper than my "no badgeless
  window".
- **Auto-migrate is idempotent and race-safe by determinism**: two
  concurrent first-views of a legacy badge both derive generation 1 → the
  *same* payload/hash (unlike a random rotate, double-fire cannot
  double-rotate). Defuses the strongest objection to the GET side effect.
- **Docs-truth minimum in THIS PR**: `qrbadge.py`'s module docstring
  ("only its SHA-256 is stored") becomes incomplete the moment this ships;
  §9.1/§9.4 gain the derivation paragraph now, not in Task F. My deferral
  of all docs to F violated the repo's shipped-docs-must-not-lie culture.

**Rejected:**
- **Invalid env → treated as absent (fall back to keyfile)**: qwen's own
  rationale ("a typo must not silently fork the derivation space")
  argues against the fix — falling back to the keyfile *is* the silent
  fork (badges derive with the unintended key; fixing the env later 409s
  them all). Fail-fast at startup (me + kimi, the `HCRM_EMBED_STRICT`
  precedent) is unambiguous and costs an operator one boot with a clear
  message.
- `view_badge` in `app/routers/auth.py` with members importing it — works
  (the `scan_requests → cart._upsert_line` precedent), but 2-of-3 put the
  lifecycle helper in `qrbadge.py` where the badge logic lives; keep it
  there.
- `detail.reason` as the 409 discriminator — the shipped Task-3 envelopes
  use `status` (`{"status": "unknown"}`); consistency wins.

## 3. Final recommended shape

- **Key**: `app/security.py`, lazy-cached `badge_secret()`; env
  `HCRM_BADGE_SECRET` (64 hex, **invalid → loud boot failure**) else
  `<data>/badge.key` (32 bytes hex, `O_CREAT|O_EXCL` 0600, announced at
  startup, auto-generated deliberately — boot never bricks); path computed
  from `HCRM_DATA_DIR` **without importing app.database** (keep-in-sync
  comment); returns `None` only when the file is uncreatable/corrupt.
- **Derivation** (`app/qrbadge.py`): frozen
  `BADGE_DERIVATION_CONTEXT = "badge"`; `derive_badge_payload(user_id,
  generation, secret)`; **KAT-pinned**; `rederive_and_check(user)`-style
  verify-before-return.
- **Model/migration**: `qr_badge_generation: int = Field(default=0)`,
  `INTEGER NOT NULL DEFAULT 0` (2-of-3; 0 = legacy/never-derived, no
  None-handling; "no badge" stays `qr_badge_hash IS NULL`); no index;
  exact-set + legacy-lands-0 + **legacy-hash-still-logs-in-post-upgrade**
  pins.
- **Issuance**: `new_badge_for` = generation++ / derive / store hash;
  **ValueError on unflushed id** (not assert); random-payload fallback
  with generation left 0 when the secret is `None` (creation never
  blocks); register/create_member = add → flush (IntegrityError→409 now
  at flush) → derive → commit — one transaction, no badgeless window.
- **Re-view**: `view_badge(session, user)` in `qrbadge.py`; 404
  `{status: "no-badge"}`; 409 `{status: "key-mismatch"}` / `{status:
  "no-key"}` (Task-3 `status` convention; login unaffected in both);
  legacy auto-migrate → 200 with `migrated: true` (idempotent by
  determinism — documented so no reviewer "fixes" the GET side effect);
  `QrBadgeViewOut {payload, svg, generation, migrated}`. Self
  `GET /api/auth/qr-badge` + staff `GET /api/members/{id}/qr-badge`
  (manage rule), neither exempt from the pw-change gate.
- **Ops**: `backup_db.sh` copies the key + credential-bearing warning
  (absent key → backup unchanged); `test_backup_and_run.py` keyfile case;
  healthz `badge_derivation` + `badge_key_source`; **minimum docs-truth in
  this PR** (qrbadge docstring + §9.1/§9.4 derivation paragraph; the full
  rewrite stays in F).
- **Tests**: the union — my 11 cases + kimi's revoke-never-reuses-a-
  generation and staff-matrix cases + qwen's KAT, unflushed-ValueError,
  no-side-effect-on-409, corrupt-file, concurrent-first-view, and
  legacy-upgrade-still-logs-in pins; the email-change-never-touches-badge
  mechanism pin here, integration pin cross-referenced to Task B.

## 4. Changes to my own proposal

1. Key loader moves `qrbadge.py` → **`security.py`**, path computed without
   the `app.database` import (qwen); `O_EXCL` discipline (kimi);
   `badge_key_source` in healthz (kimi).
2. `qr_badge_generation` **NOT NULL DEFAULT 0** instead of nullable (2-of-3).
3. Add the **KAT**, the **frozen context constant**, `rederive_and_check`
   as a named self-test, **ValueError over assert**, the
   **idempotent-auto-migrate** documentation, the **RegisterOut 409-after-
   creation** justification, the **docs-truth minimum in this PR**, and
   the backup-test extension.
4. 409 discriminator standardised on `status` (Task-3 convention), not
   `reason`.
5. Rejected from peers, with reasons recorded: qwen's invalid-env→
   keyfile fallback (the silent fork it warns about), qwen's
   `view_badge`-in-router placement, kimi's missing no-key fallback.
