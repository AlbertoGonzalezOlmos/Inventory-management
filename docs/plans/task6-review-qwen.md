# Task 6 review (qwen3.8-max) — peer proposals for the closing pass

Reviews `task6-proposal-{glm,kimi}.md` against `task6-proposal-qwen.md`.
Every peer claim below was **re-verified against the tree this pass** (line
numbers cited), per the repo's prove-don't-assume culture. §4 is the merged
fix+docs+test plan; §5 the changes to my own proposal.

---

## 1. Review of `task6-proposal-glm.md`

### Strengths (adopt)

- **S1 — F1: the account view's inline badge print is not isolated**
  (verified: `index.html:251/256` renders `badgeSvg` inline with a plain
  `window.print()`; the `printing-badge` body class is toggled only by the
  `badgePrint`/`memberBadge` watchers). Printing a rotated own-badge yields
  the whole account page around the credential. Not a leak (own badge,
  on-screen already — kimi's calibration is right) but it is the one badge
  print path the Task-5 isolation machinery misses, and the fix is ~10
  frontend lines (`printAccountBadge()` with add-class/`window.print()`/
  finally-remove + `badge-card` class on the block). Fix beats ledger entry
  (it deletes glm's own D10). Adopt.
- **S2 — The migration-gap root cause, precisely**: `test_migrations.py`'s
  `_run()` helper calls only `_migrate_schema()`, **never `create_all()`**
  (verified at line 58–63) — so the new-tables test must replicate the real
  startup sequence (`create_all()` + `_migrate_schema()`, matching
  `_startup`). All three proposals want the test; glm's is the only one that
  explains *why* the existing fixture can't just be reused as-is. Adopt the
  precision.
- **S3 — The D1–D12 accepted-risk ledger as a single deliverable table** —
  the right shape for §11's ops section (merge with kimi's 1c rows and my
  A-table; glm's covers the most ground: deadline residue, no rate limit,
  bridge stdout, raw-mode 401 design, PUT LWW, staging, payload-in-print-
  modal, stock-0 intake, weak ref, print-without-class, HID heuristics,
  identify-under-stolen-session). Adopt as the ledger skeleton.
- **S4 — "Also verified clean" list** (badge never in URLs, `autocomplete
  ="off"` on the badge field, hash-at-rest both credential types,
  `_closeOverlays` on session end…) — useful audit-completeness record for
  the PR description; adopt the practice (with the caveat that
  `_closeOverlays`-on-session-end is *incomplete* — kimi 1b).
- **S5 — §11 content outline** (capture rules, countdown with
  server-deadline-is-truth, switch ordering, intake, attribution, env var,
  Phase-B-deferred pointer) — converges with mine/kimi's; glm's phrasing
  "the checklist item is only that the bridge itself still works for
  back-office use" is the correct scope statement for Track B. Adopt.
- **S6 — README false-claims-test compatibility check** (the pinned phrases
  — BEGIN IMMEDIATE warning, unsafe-eval, jsdelivr — verified editable
  around). Adopt as a pre-edit gate.

### Weaknesses / risks (avoid or fix)

- **W1 — D3 accepts the bridge's full-token stdout printout**, reasoning
  "trusted back-office process, like `--password` on its own CLI". The
  analogy is weak: `--password` *warns* about process-list exposure (the
  bridge itself nags), while the token printout is silent, and **post-Task 2
  every account has a badge**, so badge-login token printing went from a
  rare headless convenience to the default outcome of any badge scanned in
  bridge mode — into scrollback, `tee`, journald, CI logs. This is my FIX-1;
  kimi also leaves it unchanged. **1-vs-2 — I keep recommending the
  redaction** (text mode masks `3f9a1c2d…`, `--show-token` and `--json`
  keep the full value; the pinned record contract is untouched — verified
  the tests assert `badge_login()`'s dict, not the emit line), but it is the
  coordinator's call; fallback = D3 ledger entry + a loud docstring warning.
- **W2 — Section numbering off by one**: glm inserts "§11 before References,
  which renumbers to §12" — References *is* §10 today, so the new section is
  **§10** and References becomes §11 (kimi has it right). Also grep for
  inbound "§10"/"References" cross-links before renumbering.
- **W3 — Missed kimi's two fixes** (pill payload, stranded modals on
  session-death paths) — glm's audit grepped producers/URLs/logs but not the
  SPA's *display* surfaces beyond the modals; its D-ledger even lists the
  pill nowhere. Not a flaw in what glm wrote, but the merged plan must carry
  kimi 1a/1b.
- **W4 — Misses the README tracked-issues falsehood at the line level**:
  glm fixes "phrasing where it implies client-side only (line ~122 area)"
  but the *tracked-issues table* row (verified `README.md:400`: "The
  purchase list is client-side only") and the line-406 bullet are flatly
  false now — the repo has a README-truth commit culture (`1245085`); my
  FIX-2 names them explicitly. Merge: fix both locations.
- **W5 — No NOTES.md refresh** (kimi has it): `NOTES.md:263` still describes
  the client-side list with "Checkout is a placeholder toast". One paragraph.

## 2. Review of `task6-proposal-kimi.md`

### Strengths (adopt)

- **S1 — FIX 1a: the scan pill displays badge payloads in cleartext**
  (verified `app.js:789`: `scanFlash = "scan: " + code` for *every* scan;
  the pill renders it for 2 s). A `HCRM1:…` bearer credential on a kiosk
  screen is shoulder-surfable and violates the badge doctrine (shown once,
  at issue) — the best catch of this review round, missed by glm and by my
  own A1 (which audited producers, URLs and logs, but not SPA display
  surfaces past the modals). Fix as proposed: mask on *shape*
  (`BADGE_FULL_RE || BADGE_RAW_RE` → `"scan: badge received"`), barcodes
  stay verbatim (not secrets; the pill's misroute-visibility purpose is
  preserved). Manual-checklist item 6 pins it.
- **S2 — FIX 1b: session-death paths strand modals over the login screen**
  (verified: `hcrm-session-expired` handler at `app.js:1621-1623` and the
  raw-path `handleSessionExpired` both clear user/logoff/route but never
  call `_closeOverlays()` — which only `logout()` and `switchAccount()`
  do). A token dying while the intake/detail/badge modal is open leaves a
  user-specific dialog floating over the login view, whose buttons then
  401-loop. Two-line fix (add `_closeOverlays()` to both handlers). Adopt.
- **S3 — Correct section numbering** (new §10, References → §11) and the
  concrete `docs/barcodes.md` placement: extend the **existing §8 "How this
  is wired into HCRM"** (verified at line 184) with a Suggestions
  subsection — better than my/glm's free-floating new section.
- **S4 — NOTES.md paragraph refresh** (W5 above) + the ops doc notes:
  body-logging middleware must exclude `/auth/*` and `/cart/scan`;
  embeddings-down means intake queues as ScanRequests until recovery;
  legacy-import "first logger-in on that browser" semantics. Adopt all into
  §11/ledger.
- **S5 — `[BLOCKED]` markers inline in the checklist** with the
  `SCANNER-AGENT-SYNC.md` pointers, and the correct nuance that the udev
  rule is needed for *hidraw capture / USB-COM*, not for the HID-keyboard
  path that works today. Adopt the marking convention.
- **S6 — "Checklist output recorded in the PR"** as evidence for the manual
  gate. Adopt.
- **S7 — Correct calibration of the account-page print** ("nothing beyond
  the badge itself is sensitive") — keeps glm's F1 honest as polish, not
  security.

### Weaknesses / risks (avoid or fix)

- **W1 — Leaves the bridge token printout unchanged** ("existing bridge
  behaviour, unchanged by Tasks 1–6"): true but not a justification — Task 6
  is exactly where a shipped-by-inheritance behaviour gets audited. See glm
  W1; contested 2-vs-1 against me, coordinator decides.
- **W2 — No smoke-test coverage** of the kiosk loop (my Part 5): kimi's
  verification story is pytest + ui_check + manual checklist. Defensible
  scope discipline; I keep mine as **optional** (see §4).
- **W3 — Skips the README tracked-issues row** (adds only an env row + one
  feature line): `README.md:400/406` stay false. Merge with my FIX-2/glm's
  phrasing fix.
- **W4 — Accepts the account-print inconsistency as doc-only** — glm's
  10-line fix is cheap enough to just do (S1 above); ledger entries should
  be for things we *can't* or *deliberately won't* fix, not for 10-line
  frontend gaps.
- **W5 — Minor**: the pill fix's verification note ("ui_check code-shape
  comment") is vague — ui_check cannot see the pill (it renders only after
  a scan, and the harness cannot type); the honest gate is the manual
  checklist item + code review. State it that way.

## 3. Disagreements and verdicts

| Topic | glm | kimi | qwen | Verdict |
|---|---|---|---|---|
| Bridge text-mode token redaction | accept (D3) | unchanged | **fix (redact + `--show-token`)** | **Contested 1-vs-2 → coordinator.** I hold: default-safe with bearer credentials; zero contract break (record/`--json` pinned unchanged); fallback = ledger + docstring warning |
| Pill payload mask | missed | **fix** | missed | **kimi — fix** (S1) |
| Session-death modal stranding | missed | **fix** | missed | **kimi — fix** (S2) |
| Account-view print isolation | **fix** | doc-only | missed | **glm — fix** (S1; 10 lines, deletes a ledger row) |
| badgePrint-vs-intake print precedence | missed | missed (pill/toast analysis only) | **fix (3 lines)** | **Mine** — the double-backdrop print edge is real (Task-5 verdict observation); re-queue intake when badgePrint opens |
| Legacy new-tables test | **all three** | all three | all three | Converged; **glm's `_run()`-never-calls-`create_all` precision** shapes it |
| README tracked-issues row | phrasing only | env row only | **line-level fix (400/406)** | **Mine** (W4s of both peers) |
| NOTES.md refresh | no | **yes** | no | **kimi** (S4) |
| § numbering | §11→§12 (wrong) | **§10→§11** | ambiguous | **kimi** (S3) |
| barcodes.md placement | new section | **§8 subsection** | new section | **kimi** (S3) |
| Smoke kiosk loop | no | no | optional | **Mine, marked optional** (droppable for scope) |
| Risk ledger shape | **D1–D12 table** | 1c table | A1–A13 table | **glm skeleton + kimi's ops rows + my A7/A11 rows**, merged into one §10 ledger |

## 4. Final recommended plan (merged)

**Fixes (all frontend/tests; no backend behaviour changes expected):**
1. `static/app.js` — **pill masking** (kimi 1a): badge-shaped codes →
   `"scan: badge received"`; barcodes verbatim.
2. `static/app.js` — **`_closeOverlays()` in both session-death handlers**
   (kimi 1b): the `hcrm-session-expired` listener and `handleSessionExpired`.
3. `static/app.js` + `index.html` — **account-view print isolation**
   (glm F1): `printAccountBadge()` (add class → print → finally remove),
   `badge-card` class on the inline badge block, button rewired.
4. `static/app.js` — **badgePrint print precedence** (mine A8): setting
   `badgePrint` while `scanIntake` is open unshifts the intake back onto
   `intakeQueue` and nulls it (reopens on badge-modal close).
5. `tests/test_migrations.py` — **`test_new_tables_land_on_a_legacy_database`**
   (all three, glm's precision): legacy DB → run the *real startup
   sequence* (`create_all()` + `_migrate_schema()`, since `_run()` alone
   never creates tables) → assert `cart_items` + `scan_requests` in
   `sqlite_master`, `uq_cart_items_user_item` in `PRAGMA index_list`,
   duplicate `(user_id, item_id)` insert raises `IntegrityError`, idempotent
   re-run.
6. **Contested (coordinator):** `scripts/scanner_bridge.py` text-mode token
   redaction + `--show-token` + stderr note (+ emit tests; record/`--json`
   untouched). Fallback: ledger D3 + docstring warning.

**Docs:** `docs/opticon-m10.md` new **§10 "Counter & kiosk flows"**
(References → §11; grep inbound §-links first): transports (HID today /
USB-COM bridge; **Phase-B relay deferred — not checklisted**), capture
contract, badge flows (server deadline is truth; lazy expiry), switch
ordering, intake/attribution, `HCRM_LOGOFF_GRACE_SECONDS`, the **merged
accepted-risk ledger** (glm D1–D12 ⊕ kimi ops notes: body-logging exclusion
for `/auth/*` + `/cart/scan`, embeddings-down queues ScanRequests,
legacy-import first-logger-in semantics ⊕ my A7 api()-token rationale, A11
no-credentials-in-query-strings), and the hardware checklist as an appendix
with kimi's `[BLOCKED]` markers. `README.md`: env row, feature bullets
(server-side per-user cart, scan-to-cart, kiosk flows), **tracked-issues
row 400 + bullet 406 rewritten** (checkout remains the non-goal; the list
is server-side), pre-checked against `test_readme_has_no_known_false_claims`
pinned phrases. `docs/barcodes.md`: **§8 subsection** "Suggestions"
(rank_prefix_siblings rule + why-not-ladder, keyword dialect, semantic
degrade, misreads never suggested, one worked example). `NOTES.md`: one
paragraph replacing the stale client-side-list description.

**Optional (droppable for scope):** `scripts/smoke_test.py` kiosk-loop
checks (register+badge → identify → schedule/cancel → scan added/404/422 →
cart totals → cleanup) — live-HTTP confidence beyond TestClient.

**Gates:** `uv run pytest` green (494+ with the new migration test),
`ui_check.py --self-host` green, `node --check static/app.js`, README
false-claims test green, and the HID checklist executed with output pasted
into the PR description (kimi S6); `[BLOCKED]` Track-B items recorded with
the `SCANNER-AGENT-SYNC.md` pre-flight (xHCI remove+rescan — no FLR,
`reset_method=pm`; udev rule; ModemManager ignore; **`power/control=on`
autosuspend prevention** — my pre-flight item, absent from both peers).

## 5. Changes to my own proposal

1. **Adopt kimi 1a + 1b as Fixes 1–2** — both real, both missed by my
   audit (A1 covered producers/URLs/logs but not SPA *display* surfaces
   beyond the modals; A7 traced `api()` semantics but not the
   session-death handlers' overlay hygiene). My audit table gains a rule:
   grep every render binding of credential-shaped state, not just its
   producers.
2. **Adopt glm F1** (account-print isolation) — missed; my A8 was the
   *other* print edge (double backdrop), kept as Fix 4.
3. **Adopt glm's `_run()`/`create_all()` precision** for the migration test
   (my Part 3 said "reuse the machinery" — the machinery alone doesn't
   create tables).
4. **Adopt kimi's numbering (§10), barcodes.md §8 placement, NOTES.md
   refresh, [BLOCKED] convention, PR-evidence rule, and ops doc notes.**
5. **Bridge redaction: keep recommending, honestly marked contested
   (1-vs-2)** with the fallback ledger path — no longer presented as a
   settled FIX.
6. **Smoke additions demoted to explicitly optional** (both peers scoped
   them out; keep as a coordinator opt-in).
7. **Keep** (against both peers): README line-400/406 tracked-issues fix;
   A8 print-precedence; autosuspend-prevention pre-flight step.

## 6. Verdict

kimi's proposal is the strongest base — it found the only two *behavioural*
bugs still in the tree (pill credential display, stranded modals) that both
other audits missed — with glm contributing the best ledger shape, the
account-print fix, and the migration-fixture root cause, and mine
contributing the README truth fix, the print-precedence edge, the
contested bridge-redaction hardening, and the Track-B pre-flight
completeness. §4 is the merged plan I recommend; the single open decision
for the coordinator is the bridge text-mode token redaction (recommend yes;
documented fallback provided).
