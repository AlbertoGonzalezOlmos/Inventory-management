# Task A review (qwen3.8-max) — peer proposals for derived re-viewable badges

Reviews `a-proposal-{glm,kimi}.md` against `a-proposal-qwen.md`. The three
are close: identical formula, flush-then-derive single transaction, hash
stays the login authority, legacy auto-migrate with a `migrated` flag,
structured 409s, staff GET behind `_get_manageable_member`, gate not
exempt, env-precedence + O_EXCL 0600 keyfile + startup bootstrap,
backup-copy + credential-bearing warning, healthz flag, exact-set +1, and
the "never derived from email" pin (all three independently — the Task-B
safety property). The disagreements that matter are seven; verdicts below,
merged shape in §4.

---

## 1. glm — strengths / weaknesses

**Adopt:**
- **S1 — Fail-fast on a set-but-invalid `HCRM_BADGE_SECRET`**
  (`BadgeSecretError` at boot, the `HCRM_EMBED_STRICT` pattern). This beats
  my "warn + fall back to file": a silent fallback derives every badge from
  a key the operator did *not* choose, and the divergence explodes later
  (operator fixes the typo → every file-key badge 409s `key-mismatch` →
  surprise mass re-issue). Loud boot failure on an explicit-but-broken
  config is the repo's doctrine (migration identifier validation, run.sh
  refusals). Absent env → file fallback stays (all three agree).
- **S2 — `view_badge` raises a domain error (`BadgeViewError(reason)`),
  routers map to HTTP** — keeps `qrbadge.py` free of FastAPI/schema imports
  (pure badge-domain module), which kimi's version (returning
  `QrBadgeViewOut`) would couple. Adopt the layering; helper placement in
  `qrbadge.py` is 2-of-1 (glm+kimi) over my routers/auth.py — conceded,
  with the reason→HTTP mapper shared from auth.py (members.py imports it,
  the `scan_requests→cart` precedent).
- **S3 — The auto-migrate escape hatch, stated as a decision condition**:
  "if real printed legacy cards exist in the field, flip the legacy branch
  to `BadgeViewError('legacy')` — two lines." The best framing of the
  coordinator-adjudicated risk; adopt verbatim into the helper docstring.
- **S4 — Smoke checks ride with Task A** (+2: re-view round-trip,
  `healthz.badge_derivation`) rather than waiting for Task F — the feature
  gets live-HTTP cover from its own PR. Adopt (my plan left smoke in F).
- **S5 — "exactly four `new_badge_for` call sites — verified"** and the
  reviewer instruction to re-read the register 409 block since the
  IntegrityError moves from commit to flush. Adopt as PR-review checklist.

**Reject/fix:**
- **W1 — `assert user.id is not None`** in `new_badge_for`: asserts are
  stripped under `python -O`; house style for bad programmer input is
  `raise ValueError` (the `classify_gtin` precedent; same flag I raised on
  glm's Task-6… Task-3 `prefix_candidates` assert). Keep my ValueError +
  its loud-fail test.
- **W2 — nullable `qr_badge_generation`** (NULL = legacy): 2-of-1 for
  NOT NULL DEFAULT 0 (mine+kimi) — no None-branch arithmetic, the DDL
  default lands every existing row at "legacy" for free, and the no-key
  issuance fallback keeps 0 coherently.
- **W3 — No corrupt-keyfile degradation path**: glm's loader validates the
  *env* strictly (good, S1) but a corrupt/unreadable *file* behaviour is
  unspecified; with `badge_secret() -> bytes` non-Optional (glm's signature
  raises), a corrupt file at issuance time would 500 a registration. Keep
  the Optional + random-gen-0 fallback for the FILE path (mine+glm's own §1
  fallback paragraph actually agrees — the signature just contradicts it).
  Net rule: **invalid env = boot failure (S1); unavailable/corrupt file =
  degrade to random issuance + `no-key` 409 on re-view** (registration
  never bricks on key I/O).
- **W4 — No frozen known-answer test**: glm's derivation tests pin
  determinism/distinctness but not the exact HMAC output. The formula is a
  forever-contract (any change orphans every stored hash); a KAT with a
  fixed secret/id/generation is the only pin that catches a "harmless
  refactor" of the message string. Keep mine (kimi also lacks it).
- **W5 — Defer-all-docs to F** leaves §9.4's shipped claim ("only its
  SHA-256 hash is stored server-side") incomplete the day A merges —
  against the docs-with-code culture (README-truth commits). Keep my
  minimum: 2–3 true sentences in §9.1/§9.4 + the `qrbadge.py` module
  docstring in A's PR; F does the full rewrite + runbook. (Contested
  1-vs-2 — kimi also defers; coordinator call, cheap either way.)

## 2. kimi — strengths / weaknesses

**Adopt:**
- **S1 — The revoke/monotonicity interaction, named**: "Revoke keeps the
  generation (never decrement): a revoked-then-reissued badge never equals
  an older one." This is a real resurrection hazard — if revoke ever reset
  the generation, the next issue would re-derive a *dead* payload whose
  hash matches an old printed card. My plan said "monotonic" but never
  pinned the revoke path; kimi's test 5 (revoke → re-issue → generation 3,
  never reuses 1/2) is the pin. Adopt rule + test.
- **S2 — Env-override test asserts NO keyfile is written** (explicit-env
  deployments must not sprout a stray file key that a future boot might
  prefer after the env is removed — wait, env always wins by precedence,
  but a stale file is exactly the confusion the test prevents). Adopt.
- **S3 — The fullest wrong-key boot test**: boot with secret A → issue →
  reboot with B → GET 409 `key-mismatch` **and** a B-derived payload 401s
  **and** the real A-badge still 200s (hash authority) — three assertions
  in one subprocess case. Adopt as the canonical version.
- **S4 — `compare_digest` for the re-view hash verification** (doctrine
  parity with `verify_password`; my `rederive_and_check` implied plain
  `==`). Adopt explicitly.
- **S5 — Legacy-DB-with-a-live-random-badge-still-logs-in-post-upgrade**
  migration test (stronger than the column-lands pin alone). Adopt.
- **S6 — `migrated` (short field name)** over my `migrated_from_legacy`
  (2-of-1 with glm). Adopt; keep `generation` in the response (mine+kimi
  2-of-1 over glm).

**Reject/fix:**
- **W1 — `badge_secret() -> bytes` (never None)**: "auto-generate because
  the server must boot" is right for a *missing* file, but kimi's signature
  has no degraded state at all — an unwritable data dir with an external
  `DATABASE_URL` turns every registration into a 500, and their error table
  has no `no-key` row. Keep Optional + random fallback + `no-key` 409
  (mine+glm).
- **W2 — `view_badge` returns `QrBadgeViewOut`** from `qrbadge.py` —
  couples the badge module to pydantic schemas; glm's domain-error
  layering is cleaner (S2 above).
- **W3 — `from app.database import DATA_DIR` at security-module top
  level**: acyclic as kimi verifies, but it drags the
  database→embeddings→fastembed import chain into every consumer of
  `security.py` (which `qrbadge.py` — and thus both auth routers — already
  imports). Neither kimi's import nor my duplicated env-resolution is
  needed: **function-level `from app.database import DATA_DIR` inside the
  loader** — single source of truth, zero import-time coupling (the
  repo's established lazy-import pattern: `database.py`'s function-level
  `from app import models`, `main.py`'s local imports). Resolves my
  R-with-kimi in one line.
- **W4 — No KAT** (shared with glm — see glm W4).
- **W5 — error-detail discriminator `status` vs my `reason`**: kimi+glm use
  `{"status": "key-mismatch"|"no-key"|"none"}` — and `status` is the
  **shipped house convention** (Task 3/4 details: `{"status":
  "already"|"duplicate"|"not_open"|"misread"|"badge"}`). I was the outlier;
  adopt `status` (with `"no-badge"` over glm's `"none"` for clarity).

## 3. Remaining disagreements (quick verdicts)

| Topic | glm | kimi | qwen | Verdict |
|---|---|---|---|---|
| Invalid env secret | **fail-fast boot** | unspecified | warn+fallback | **glm** (S1) |
| Column nullability | NULL legacy | **0 NOT NULL** | **0 NOT NULL** | 2-of-1 → 0-based |
| Key-loader module | qrbadge.py | **security.py** | **security.py** | 2-of-1 → security.py (env-knob home) |
| DATA_DIR access | lazy db import | top-level import | mirrored path calc | **function-level import** (kimi W3 resolution) |
| view helper home + shape | **qrbadge + domain error** | qrbadge + schema return | auth.py router helper | **glm** (S2), mapper shared from auth.py |
| No-secret state | Optional fallback | never None | Optional fallback | **Optional** (kimi W1) |
| id-guard | assert | (implicit) | **ValueError** | ValueError (glm W1) |
| Response fields | payload/svg/migrated | +generation | +generation | `{payload, svg, generation, migrated}` |
| Detail discriminator | status | **status** | reason | **status** (W5 — I concede) |
| KAT | none | none | **frozen vector** | **mine** (glm W4) |
| Smoke timing | **with A** | with A | with F | **with A** (glm S4) |
| Docs in A's PR | defer to F | defer to F | **minimum truth-fix now** | **mine, contested 1-vs-2** — coordinator call |
| Revoke keeps generation | implicit | **explicit + test** | implicit | **kimi** (S1) |

## 4. Final recommended shape (deltas over the convergent base)

- `app/security.py`: `badge_secret() -> bytes | None` (cached module
  global; **env precedence with fail-fast `BadgeSecretError` on set-but-
  invalid**; else `<DATA_DIR>/badge.key` via function-level DATA_DIR
  import, `O_CREAT|O_EXCL` 0600, `secrets.token_hex(32)`; corrupt/
  unwritable file → None, logged); `badge_secret_source() -> "env"|"file"|
  None`; bootstrap + announcement in `_startup`; healthz +=
  `badge_derivation: bool`, `badge_key_source`.
- `app/qrbadge.py`: `derive_badge_payload(user_id, generation, secret)`
  (pure, explicit-secret signature — the KAT target); `new_badge_for(user)`
  (generation `(current or 0)+1`; `ValueError` if `user.id is None` on the
  derived path; random gen-0 fallback when `badge_secret()` is None, loud
  log); `rederive_and_check(user)` with `compare_digest`;
  `view_badge(session, user) -> (payload, migrated)` raising
  `BadgeViewError("no-badge"|"no-key"|"key-mismatch")`, legacy branch
  auto-migrates with glm's two-line-flip escape hatch documented;
  **revoke never touches the generation** (kimi S1, pinned).
- Model/migration: `qr_badge_generation: int = Field(default=0)`,
  `("users","qr_badge_generation","INTEGER NOT NULL DEFAULT 0", ())`, no
  index; exact-set += 1.
- Issuance: `add → try: flush → new_badge_for → commit / except
  IntegrityError: rollback → 409` in register + create_member (single
  transaction; the raise point moves commit→flush — re-read the block per
  glm S5); rotate endpoints unchanged call-site-wise.
- Endpoints: `GET /api/auth/qr-badge` (self) + `GET /api/members/{id}/
  qr-badge` (staff, `_get_manageable_member`); shared reason→HTTP mapper in
  auth.py; responses `QrBadgeViewOut {payload, svg, generation, migrated}`;
  errors 404 `{status:"no-badge"}`, 409 `{status:"no-key"|"key-mismatch"}`
  with "logins are unaffected" in every message; gate applies (not exempt).
- Backup: keyfile copy + credential-bearing warning + restore-note;
  `test_backup_and_run.py` case. Smoke += re-view round-trip + healthz
  flag (rides with A).
- Tests: the merged list — KAT (frozen vector); determinism/distinctness;
  ValueError-on-unflushed; register/create gen-1 + login both forms +
  identify; re-view repeatability (== register payload); rotate kills old;
  **revoke→re-issue never reuses a generation**; wrong-key trio
  (monkeypatch 409-no-side-effect, forged payload 401, subprocess A→B boot
  with all three kimi assertions); legacy auto-migrate (old dies,
  idempotent second GET, concurrent-first-view determinism note); no-badge
  404; no-key degradation (register 201 random gen-0, GET 409, healthz
  false, key restored → migrates); keyfile bootstrap (0600 via stat, O_EXCL
  loser reads winner, stable, corrupt-file degrade, **env-used-verbatim-
  writes-no-file**); invalid-env boot failure; flush-race 409 (no row, no
  generation consumed); staff matrix; gates; email/name-edit-in-DB → same
  payload (Task-B mechanism pin, cross-referenced); migrations exact-set +
  legacy landing + **legacy random badge still logs in post-upgrade**;
  backup keyfile case; smoke +2. `test_qr.py` existing suites green with a
  gen-1 assertion added to the creation tests.
- Docs in A's PR (recommended, contested): §9.1/§9.4 minimum truth-fix +
  `qrbadge.py` module docstring; F carries the full rewrite, README env
  row/trade-off paragraph, and the rotation runbook.

## 5. Changes to my own proposal

1. **Invalid env → fail-fast `BadgeSecretError`** (glm S1) — withdraws my
   warn-and-fallback (silent key divergence is worse than a loud boot
   failure); the file path keeps graceful degradation.
2. **`view_badge` moves to `qrbadge.py` with a domain error** (glm S2,
   2-of-1); my router-side helper survives only as the shared reason→HTTP
   mapper in auth.py.
3. **`status` detail discriminator** (house convention; withdraws `reason`).
4. **`migrated` field name** (2-of-1); keep `generation`.
5. **DATA_DIR via function-level import** — withdraws my mirrored path
   calculation (kimi W3 resolution: single source, no import-time chain).
6. **Adopt** kimi's revoke-monotonicity rule + test, env-writes-no-file
   test, three-assertion subprocess boot test, `compare_digest` in the
   re-view check, legacy-badge-post-upgrade-login migration pin; glm's
   smoke-with-A, escape-hatch docstring, four-call-sites reviewer note.
7. **Keep** (against one or both peers): KAT frozen vector; ValueError
   id-guard; `NOT NULL DEFAULT 0`; Optional secret + `no-key` 409; O_EXCL
   double-boot and corrupt-file tests; docs-truth minimum in A's PR
   (flagged contested).

## 6. Verdict

No proposal ships as-is; the merge is strictly better than each. **glm
contributes the config-failure doctrine (fail-fast env) and the cleanest
helper layering; kimi contributes the two subtlest correctness pins
(revoke-monotonicity resurrection hazard, env-writes-no-file) and the
canonical wrong-key subprocess test; mine contributes the KAT, the
degraded-file path with `no-key`, the loud ValueError, and the
docs-in-A discipline.** The single open choice for the coordinator: whether
the §9 truth-fix rides with A (recommended — shipped docs must not lie
between PRs) or waits for F. Everything else in §4 is converged or
2-of-1-decided.
