# Task A review (kimi-k3): derived badge proposals

Reviews of `a-proposal-glm.md` and `a-proposal-qwen.md` vs my own
`a-proposal-kimi.md`. The three are genuinely close: same derivation
formula, same hash-stays-authority split, same flush-then-derive issuance,
same legacy auto-migrate, same backup/healthz additions. Only the deltas
need adjudication.

## GLM

**Adopt**
- **Fail-fast on an invalid `HCRM_BADGE_SECRET`** (`BadgeSecretError` at
  startup, the `HCRM_EMBED_STRICT` pattern): a *set-but-malformed* env is a
  misconfiguration — silently falling back (qwen) or using it verbatim
  (me) forks the derivation space and turns every existing badge into a
  key-mismatch. Boot must fail loudly. Validated 64-hex (both peers) also
  beats my "used verbatim".
- **Injectable `load_badge_secret(env=, data_dir=)`** — the testable core
  behind the cached accessor; cleaner than my cache-reset seam alone.
- **Loud `ValueError`/`assert` on an unflushed `user.id`** in
  `new_badge_for` (qwen has it too) — my "REQUIRES user.id" docstring
  becomes an enforced contract.
- **Graceful issuance fallback** (secret unavailable → random payload,
  generation stays legacy, creation never bricks — qwen has the same with
  the better test). Cheap insurance for an unwritable data dir.
- **The auto-migration escape-hatch note**: if real printed legacy cards
  ever exist in the field, the legacy branch flips to `409 legacy` in two
  lines — worth recording verbatim (coordinator's auto-migrate stands for
  this codebase, where no such population exists).
- Smoke-test `+2` checks (re-view round-trip + healthz flag) — qwen has
  the same; adopt.

**Reject / note**
- **`generation NULL` for legacy** — NULL-vs-0 semantics for no benefit;
  the coordinator's spec says default 0/1 and qwen/mine use
  `INTEGER NOT NULL DEFAULT 0` (monotonic increment always defined). Keep 0.
- **Key loader in `app/qrbadge.py`** — credential-lifetime constants live
  in `app/security.py` (TOKEN_TTL, PBKDF2, DEV_ADMIN, LOGOFF_GRACE_SECONDS);
  qwen and I both put it there.
- **`BadgeViewError` + router-side mapping** — an extra layer versus
  raising the structured HTTP errors in one router helper (qwen's shape,
  backed by the `scan_requests` → `cart._upsert_line` cross-router
  precedent).

## Qwen

**Adopt (most are refinements of my own design)**
- **Frozen domain-separation context + known-answer test**:
  `BADGE_DERIVATION_CONTEXT = "badge"` and a KAT pinning
  secret/user/generation → the exact payload string. The formula is a
  forever-contract (any drift orphans every stored hash); the KAT makes
  drift unmergeable. My design lacked both — adopt.
- **`rederive_and_check(user)`** as a named self-test helper — clarity for
  the "never serve a wrong badge" property.
- **No `app.database` import in `security.py`** — duplicate the two-line
  `HCRM_DATA_DIR` resolution with a keep-in-sync comment instead. My R2
  said "import from database, single source of truth"; qwen's point is
  better: `app.database` pulls the embedding/extension machinery into
  every security consumer for one path constant. Adopt (reverses my note).
- **`view_badge` in `app/routers/auth.py`, imported by `members.py`** (the
  `scan_requests` precedent) raising structured HTTP errors directly —
  simpler than my qrbadge.py placement and GLM's error-mapping.
- **Auto-migration race-safety argument**: two concurrent first-views
  derive the *same* generation-1 payload by determinism — unlike a random
  rotate, double-fire cannot double-rotate. The strongest justification
  for the coordinator's GET-with-side-effect; adopt into the docstring.
- **`migrated_from_legacy` flag name** (clearer than my `migrated`).
- **Docs-truth in this PR**: §9.1/§9.4 derivation paragraph + the
  qrbadge.py module docstring must land with Task A (docs must not keep
  promising "hash-only, shown once" after merge; the full rewrite is Task
  F). Adopt — house README-truth culture.
- **`.gitignore` verification** for `badge.key` (test_repo_hygiene stays
  green) — I assumed it; qwen pins it.
- **Two-phase rejection reasoning**: `RegisterOut` *requires* `qr_badge`,
  so a two-phase phase-2 failure 500s *after* creating the account —
  another point for flush (already my position).
- **`badge_secret_source` in healthz**, corrupt-file/corrupt-env test
  cases, backup test's absent-keyfile branch.

**Reject / note**
- **Invalid env treated as absent + warning** — see GLM above; fail-fast
  wins.
- **`reason` as the error discriminator** — the house convention from
  Task 3 is `status` (`{"status": "misread"}`, `{"status": "already"}`,
  `{"status": "duplicate"}`); qwen mislabels it as the envelope
  convention. Keep **`status`** (mine and GLM's).

## Disagreement scoreboard

| Point | Me | GLM | Qwen | Verdict |
|---|---|---|---|---|
| Key loader home | security.py | qrbadge.py | security.py | **security.py** |
| DATA_DIR source | import database | (lazy import) | duplicate + sync comment | **qwen** (import weight) |
| Invalid env | verbatim | fail-fast boot | warn + treat as absent | **GLM** (fail-fast) |
| Legacy marker | 0 | NULL | 0 | **0** (coordinator spec) |
| view_badge home | qrbadge.py | qrbadge.py + errors | auth.py, cross-import | **qwen** (precedent) |
| Error discriminator | `status` | `status` | `reason` | **`status`** (Task-3 convention) |
| Migration flag | `migrated` | `migrated` | `migrated_from_legacy` | **qwen** |
| Unflushed id | doc requirement | loud raise | loud raise | **loud raise** (both) |

## Final recommended shape

**Base: qwen's proposal** (frozen context + KAT, `rederive_and_check`,
auth.py helper placement, docs-truth-in-PR, hygiene pin, race-safety
argument), **plus**: GLM's fail-fast env validation and injectable
`load_badge_secret(env, data_dir)` behind the cached accessor; my
`status` discriminator; generation `0` (not NULL); the merged test list
(qwen's 14 + GLM's flush-race/fallback/smoke additions — which overlap
qwen's 9 and 14 and the smoke +2).

## Changes to my own proposal

1. Add the **frozen `BADGE_DERIVATION_CONTEXT` + known-answer test**.
2. Env handling: verbatim → **validated 64-hex, fail-fast on invalid at
   startup**; add injectable loader for tests.
3. `security.py` does **not** import `app.database` — duplicate the
   DATA_DIR resolution with a sync comment (reverses my R2).
4. `view_badge` moves from `qrbadge.py` to **`app/routers/auth.py`**
   (members.py cross-imports), raising structured errors directly with
   **`status`** discriminators (`no-badge` 404 / `no-key` 409 /
   `key-mismatch` 409).
5. `migrated` → **`migrated_from_legacy`**; add the auto-migration
   race-safety (determinism) argument to the endpoint docstring.
6. Add the loud `ValueError` on unflushed `user.id`, the no-key issuance
   fallback + its test, the smoke +2 checks, the docs-truth minimum
   (§9.1/§9.4 + module docstring in this PR), and the `.gitignore`
   verification.
7. Keep from mine: security.py home, flush-then-derive (all three agree),
   `INTEGER NOT NULL DEFAULT 0`, healthz source field, the error matrix,
   the backup warning + restore note.
