# Task 6 cross-review (glm) — of `task6-proposal-kimi.md` and `task6-proposal-qwen.md`

Reviewer context: my own proposal is `docs/plans/task6-proposal-glm.md`. All
three agree on the frame (one small PR: fix what's genuinely wrong, document
the accepted risks, three docs files, one missing migration test, a hardware
checklist split into runnable-HID / blocked-USB-COM) and on the migrations
gap (the legacy new-tables test — all three found it independently). The
review's value is in the deltas — and both peers caught real issues **my
audit missed**.

---

## 1. Kimi

**Strengths (adopt — the standout catches of this round):**
- **1a: the scan pill displays badge payloads in cleartext.** My own Task-5
  code (`enqueueScan` → `scanFlash = "scan: " + code`) renders the full
  `HCRM1:…` bearer credential on the kiosk screen for two seconds —
  shoulder-surfable, directly against the badge doctrine my own earlier
  reviews enforced. My audit checked logs, URLs, and modals and missed the
  pill. Kimi's fix is exactly right, including the detail that masking must
  be **on shape, not prefix** (`BADGE_RAW_RE` too — a prefix-stripped
  presentation is still a credential). ~4 lines. **The single most important
  fix of this pass.**
- **1b: `_closeOverlays()` missing in the two other session-death paths**
  (the `hcrm-session-expired` window handler and the raw-mode
  `handleSessionExpired`) — the Task-5 fix added it to `logout`/`switch`
  only; a token dying under an open modal strands it over the login view.
  2 lines. Real; mine to own as implementer.
- **NOTES.md refresh** — the stale "client-side purchase list" line lives
  there too; I only checked README. Adopt.
- **Section numbering**: new kiosk section inserted before References as
  §10, References → §11 (no existing section renumbers except References).
  My "§11, References → §12" was off by one. Adopt kimi's.
- The clean verdict table and the body-logging-middleware exclusion note
  (§11 ops line) — good operational addition.

**Weaknesses / disagreements:**
- **The account-view print verdict (accept + doc line) vs my F1 fix.**
  Kimi's analysis is correct that nothing sensitive prints beyond the badge
  itself (name/email/role + the credential the user is printing on
  purpose) — so *accept* is defensible. But my ~10-line `printAccountBadge()`
  (toggle the body class around `window.print()`) makes every badge print a
  clean card and costs nothing; I keep it as a recommended-but-droppable
  fix rather than a hard one. Coordinator's call; no security stake either
  way.
- Kimi's bridge-token stance is implicit-accept (its 1c table doesn't list
  the bridge at all) — qwen's FIX-1 (below) is the better treatment.

## 2. Qwen

**Strengths (adopt):**
- **FIX-1: bridge text-mode token redaction** — verified this pass:
  `scanner_bridge.py:347` prints the full 7-day bearer token in text mode,
  and since Task 2 *every* account carries a badge, so the documented
  counter-bridge flow now sprays live credentials into scrollback/`tee`/
  journald. Qwen also verified the tests pin the *record* shape but not the
  text emit line — so redaction is test-safe while `--json` (the machine
  contract) stays byte-identical. Gated behind `--show-token` with one loud
  stderr note. My ledger's D3 ("trusted back-office, accepted") was too
  permissive — the repo's culture (announced dev-admin, never-log-
  credentials) points to safer-by-default. **Adopt; D3 upgrades from
  accepted-risk to fixed.**
- **FIX-2: README truth** — verified: README:400/406 still claim "the
  purchase list is client-side only", false since Tasks 1/5, in a repo with
  a literal README-truth test culture. My earlier grep used `head -5` and
  truncated exactly these lines. Adopt, including the correct rewrite
  (checkout remains the tracked non-goal; the list is server-side and
  per-user).
- **A8: `badgePrint` + `scanIntake` double-modal print edge** — both
  backdrops print. The 3-line re-queue (badge card gets print priority,
  intake reopens) is right; droppable if the coordinator prefers a doc
  line. Adopt as recommended.
- **Part 5: the smoke-test gap** — `scripts/smoke_test.py` (the live-
  subprocess e2e gate) has zero cart/scan coverage; ~8 checks covering the
  whole kiosk loop over real HTTP (register→badge login→scan→identify→
  schedule/cancel→cleanup). I didn't think of it; it's the right
  complement to the TestClient suite. Adopt.
- The audit-table methodology (expected verdict stated per row so the
  implementer *proves or fixes*) and the checklist extras: staging survives
  reload, the user's phone session survives the switch, the visible scan
  field handles a 3-char code the gate ignores, the pre-flight including
  the ModemManager ignore and `power/control=on` suspend-resume prevention,
  and the explicit "don't test the unbuilt Phase-B relay" note.

**Weaknesses / disagreements:**
- FIX-1's compatibility cost (shell scripts parsing `session token:` from
  text mode break) is real but correctly judged — that parsing was always
  fragile and `--json` is the contract. No change.
- Missed kimi's 1a (the pill) and 1b (the stranded modals) entirely — the
  two most user-visible leaks are kimi's; the two most operational ones are
  qwen's. Complementary.
- "New §11 (before References)" numbering is self-inconsistent (a section
  inserted before the current §10 References cannot also be §11 without
  renumbering); kimi's numbering is the clean one.

## 3. Final recommended plan (one small PR)

**Fixes (all small, ordered by importance):**
1. **Pill masking** [kimi 1a] — badge-shaped codes render
   "scan: badge received"; ~4 lines in `enqueueScan`.
2. **`_closeOverlays()` in both session-expired handlers** [kimi 1b] — 2
   lines.
3. **Bridge text-mode token redaction + `--show-token`** [qwen FIX-1] —
   record/`--json` untouched; stderr note; `--help`/docstring/§6a updates;
   emit-level tests.
4. **README tracked-issues truth** [qwen FIX-2] + env row
   (`HCRM_LOGOFF_GRACE_SECONDS`) + feature bullets + "Scanning at the
   counter" pointer.
5. **badgePrint-over-scanIntake print precedence** [qwen A8] — 3 lines,
   droppable.
6. **Account-view badge print isolation** [my F1] — ~10 lines, droppable
   (kimi's accept-and-document is the fallback).

**Tests:** the legacy new-tables migration case (all three converge:
`create_all()` + `_migrate_schema()` on a legacy DB → `cart_items` with
`uq_cart_items_user_item` (duplicate insert → `IntegrityError`) +
`scan_requests`, idempotent); bridge redaction emit tests; the smoke-test
kiosk loop (~8 checks). Full `uv run pytest` + `ui_check.py --self-host` +
`node --check` as the gate.

**Docs:** `opticon-m10.md` — new kiosk/counter section **before References**
(kimi's numbering: it becomes §10, References → §11), linking (not
duplicating) §6a/§9: transports, capture contract, badge flows (server
deadline = truth; lazy expiry), switch ordering, barcode flows, config
(`HCRM_LOGOFF_GRACE_SECONDS`, `--show-token`), the accepted-risks ledger,
the hardware checklist appendix. `README.md` per fix 4. `docs/barcodes.md` —
suggestions section (indicator/zero-stripped significant view, ≥6 floor,
keyword dialect, semantic degrade-to-`[]`, misreads never suggested, one
worked example). `NOTES.md` — the stale purchase-list paragraph [kimi].

**Accepted-as-risk ledger (documented, unchanged):** my D1–D12 minus D3
(now fixed) and D10 (closed by fixes 5/6), plus kimi's body-logging
exclusion note and qwen's A-rows — one consolidated table in the new
section.

**Migrations:** verify the exact set (4 columns + 1 index, pinned) and the
legacy-column NULL pins; add the one missing test; no production changes.

**Hardware checklist:** the union — qwen's Track A (11 steps, the most
complete: includes the pill-masking check from kimi's step 6, staging
reload survival, phone-session survival, the 3-char visible-field case,
print isolation) + Track B blocked items with the full pre-flight chain
(xHCI revival/USB-C move, udev rule, ModemManager ignore,
`power/control=on`) + the explicit Phase-B-not-built note.

## 4. Changes to my own proposal as a result

1. **Add fixes 1–4** (pill masking, stranded-modal close, bridge
   redaction, README truth) — the first two are bugs in my own Task-5 code
   that my audit missed; the third upgrades my D3 from accepted-risk to
   fixed; the fourth my grep truncation hid.
2. Adopt qwen's **smoke-test additions** and **A8**; keep my **F1** but
   demote it to droppable (kimi's accept is defensible).
3. Adopt kimi's **NOTES.md** item and the **§10/§11 numbering**
   (correcting my off-by-one).
4. Ledger maintenance: D3 → fixed; D10 → closed by A8/F1; add the
   body-logging exclusion note.
5. Keep (all three agree): the migrations gap + test, the three docs
   files' substance, the HID-now/USB-COM-blocked checklist split, and the
   one-PR sequencing with the prove-or-fix audit-table methodology.
