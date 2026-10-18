# Task 6 proposal review (kimi-k3)

Reviews of `task6-proposal-glm.md` and `task6-proposal-qwen.md` vs my own.
The three converge on structure: small fix set + accepted-risk ledger +
opticon-m10 §11 + README env row + barcodes.md suggestions + one
migrations test + hardware checklist. Notably, **each proposal found
genuine issues the other two missed** — the merge is strictly additive.

## GLM

**Adopt**
- **F1 — account-view badge print isolation as a FIX, not an acceptance.**
  I had the non-isolated account-page print as "accept + doc line"; GLM's
  ~10-line `printAccountBadge()` (toggle `printing-badge`, print, remove in
  `finally` + `badge-card` class on the inline block) is cheap and removes
  the one path where a credential prints with account details (name/email/
  role) around it. Adopt — upgrades my acceptance to a fix.
- **The precise migrations-test mechanism**: the legacy suite's `_run()`
  helper only calls `_migrate_schema()`, **never `create_all()`** — so the
  new-tables test must run the *real startup sequence* (`create_all()` +
  `_migrate_schema()`), plus the `uq_cart_items_user_item` PRAGMA check and
  the behavioural IntegrityError pin. Sharpens my (and qwen's) test sketch.
- **The D1–D12 accepted-risk ledger** — broader than my table (adds:
  bridge token printing, no rate-limit rationale, payload-as-text in the
  print modal, stock-0 intake items, stolen-session identify). Adopt as
  the ledger's spine.
- **README false-claims test awareness** (verified phrase-compatible
  phrases to preserve) and the **worked example** for barcodes.md.
- **§ numbering**: new §11, References → §12 (qwen agrees; my §10/§11 was
  off by one — adopt).

**Reject / note**
- **D3 (bridge token printing) as accept-and-document** — superseded by
  qwen's FIX-1 redaction (below); acceptance is weaker than needed for a
  bearer credential in scrollback/journald.
- GLM's "verified clean" list claims `_closeOverlays` on session end —
  but it missed that the `hcrm-session-expired` window handler and
  `handleSessionExpired` don't call it (my fix 1b stands). GLM also missed
  the scan-pill badge leak entirely.

## Qwen

**Adopt (all three fixes are genuine)**
- **FIX-1 — redact the bridge's text-mode token printout.** Since Task 2
  *every* account has a badge, so the documented badge mode now prints live
  7-day bearer tokens to stdout/journald/CI logs. Qwen's shape is right:
  redact by default in text mode, `--show-token` to restore, `--json` and
  the pinned `badge_login()` record contract byte-identical (verified: the
  tests pin the record, not the emit line), one loud stderr note, docs +
  `--help` updated, emit-level tests. Better than GLM's accept-and-
  document — safer-by-default for exactly the credential class the repo
  hashes at rest.
- **FIX-2 — README truth**: `README.md:400/406` still claims "the purchase
  list is client-side only" — false since Task 1/5. My proposal updated
  feature bullets and NOTES.md but missed the stale tracked-issues claim.
  Adopt (the repo's README-truth culture makes this a fix, not a nit).
- **A8 — badgePrint + scanIntake double-modal print edge** (register opens
  the badge modal while a staged-unknown replay opens intake): both
  backdrops print. The 3-line fix (opening `badgePrint` re-queues an open
  `scanIntake` onto `intakeQueue`) is correct and droppable-cheap. Adopt.
- **The audit-table discipline** (A1–A13: each line has an expected
  verdict and must be *proved or fixed*, evidence in the PR description) —
  adopt as the PR's audit artefact, merged with GLM's ledger.
- **Smoke-test kiosk loop** (~8 checks in `scripts/smoke_test.py`: register
  badge, scan 200/404/422, cart totals, identify, schedule+cancel) — the
  live-subserver e2e gate has zero cart/scan coverage; cheap and valuable.
  Adopt (recommended, not mandatory).
- **Hardware pre-flight specifics** (unbind/bind, PCI remove+rescan,
  ModemManager ignore rule, `power/control=on`) and the **Track A / Track
  B split** — Track A (HID) independently signable, Track B (USB-COM)
  after host fixes, explicitly excluding relay testing (Phase B doesn't
  exist). Better than my blocked-items list. Adopt.

**Reject / note**
- Qwen likewise missed both of my frontend finds (below). Nothing else
  substantive; FIX-1's scope risk (breaking scripts that parse text-mode
  tokens) is correctly self-flagged and acceptable.

## Kept from my own proposal (found by neither peer)

1. **Scan-pill badge masking** — the round's most important find:
   `enqueueScan` renders `"scan: " + code` for *every* scan, so a badge
   scan paints the full `HCRM1:` bearer credential on the kiosk screen for
   2 s. Both peers audited payload *producers*; the pill is a display
   *sink*. Fix: mask badge-shaped codes (both forms) as "badge received".
2. **`_closeOverlays()` in the two session-expired handlers** (window
   `hcrm-session-expired` + raw-mode `handleSessionExpired`) — my Task-5
   fix covered logout/switch only; a token dying with a modal open
   strands it over the login screen.
3. **NOTES.md refresh** (peers cover README/barcodes/opticon-m10 only;
   NOTES is the orientation doc and makes the same stale client-side
   claim).

## Disagreements resolved

| Point | Mine | GLM | Qwen | Verdict |
|---|---|---|---|---|
| Bridge token printout | (not covered) | accept (D3) | redact + `--show-token` | **qwen FIX-1** |
| Account-page print | accept + doc | fix (F1) | (edge via A8) | **GLM F1** |
| New section number | §10, Ref→§11 | §11, Ref→§12 | §11 (slot free) | **§11, Ref→§12** |
| Stale README claim | bullets + NOTES | — | FIX-2 precise | **qwen FIX-2 + my NOTES** |
| Double-modal print | (not covered) | — | 3-line re-queue | **qwen A8** |

## Final recommended plan (one PR)

**Code fixes (5):** ① scan-pill masking (mine) · ② `_closeOverlays` in
both session-expired paths (mine) · ③ bridge text-mode token redaction +
`--show-token` + stderr note + `--help` (qwen) · ④ account-view print
isolation (GLM) · ⑤ badgePrint re-queues open scanIntake (qwen).
**Tests (3):** legacy-new-tables case through the **real startup
sequence** (`create_all()` + `_migrate_schema()`, PRAGMA unique-index
check, IntegrityError pin, idempotency — GLM's mechanism) · bridge
emit-redaction tests (record/`--json` pins untouched) · smoke kiosk-loop
checks (qwen, recommended).
**Docs:** opticon-m10 **§11** "Kiosk/counter flows" (References → §12)
with the merged accepted-risk ledger (GLM D1–D12 ∪ my table ∪ qwen's
audit table — each line evidence-backed) + the Track A/B hardware
checklist with qwen's pre-flight · README: env row + FIX-2 + feature
bullets (preserving false-claims-pinned phrases) · barcodes.md
suggestions section with GLM's worked example · NOTES.md refresh (mine).
**Gate:** `uv run pytest` green (+~3), `ui_check.py --self-host` green,
`smoke_test.py` green, audit table filled with evidence in the PR
description; Track-A checklist run and recorded, Track B marked
[BLOCKED] with the sync-file pointer.

## Changes to my own proposal

1. Upgrade account-page print from "accept + doc" to **GLM's fix**.
2. Precise the migrations test: run `create_all()` + `_migrate_schema()`
   (the legacy helper alone never calls `create_all()` — GLM's catch).
3. Section numbering: **§11**, References → §12.
4. **Add** qwen's FIX-1 (bridge redaction), FIX-2 (README stale claim),
   A8 (double-modal re-queue), the smoke-test additions, and the
   pre-flight/Track-A-B checklist structure.
5. Merge my accepted-risk table into GLM's D-ledger + qwen's audit table
   (one artefact, evidence-backed).
6. Keep my two frontend fixes and the NOTES.md refresh (neither peer has
   them).
