# Task 6 FINAL spec — Security review, docs, migrations check, hardware checklist

Merged from `task6-proposal-{glm,kimi,qwen}.md` + reviews. Implementer follows THIS.

## Code fixes (security audit outcomes)

1. **Scan-status pill masking** (`static/app.js`/`index.html`): the pill must
   never render the raw badge payload — mask `HCRM1:…` (show e.g. "badge
   scan") while keeping barcode/SKU echoes.
2. **Bridge badge redaction** (`scripts/scanner_bridge.py`): badge payloads
   must be redacted in every log/print path (emit `HCRM1:••••` or a verdict,
   never the raw secret). Add a test.
3. **Stranded overlay cleanup** (`static/app.js`): verify `_closeOverlays()`
   covers every overlay, and add the two missing close paths found in review
   (account badge-print modal; intake modal across route change).
4. **Account badge-print isolation** (`static/app.js`/`style.css`): the
   existing member/own-badge print path must also use the `.printing-badge`
   body class + `@media print` isolation (not just the one-shot modal).
5. **Print precedence** (`static/style.css`): ensure the `@media print`
   isolation block wins over base visibility rules (a real precedence fix, not
   a comment).
6. **README truth fix** (`README.md`): correct the now-stale bullet(s) so the
   false-claims test passes (badges are auto-issued at creation; the purchase
   list is server-side).

## Tests

- `tests/test_migrations.py`: add a **legacy new-tables** case — on a legacy DB
  fixture, `create_all()` lands `cart_items` and `scan_requests`; assert
  `PRAGMA index_list` shows `uq_cart_items_user_item`; a duplicate
  `(user_id, item_id)` insert raises IntegrityError; a second boot is idempotent.
- `tests/test_scanner_bridge.py` (or a new small test): bridge emits redacted
  (never raw) badge payloads on the badge path.
- `scripts/smoke_test.py`: add a compact kiosk loop (~8 live-HTTP checks over
  the public flows: register→badge→qr-identify→cart/scan→suggest→scan-request).

## Docs

- `docs/opticon-m10.md`: new §10 "Counter & kiosk flows" (capture rules, 30 s
  countdown + server-enforced lazy expiry, save-then-switch ordering, intake,
  attribution, `HCRM_LOGOFF_GRACE_SECONDS`, accepted-risks ledger, Phase-B
  deferred); renumber References accordingly.
- `README.md`: env-table row for `HCRM_LOGOFF_GRACE_SECONDS`; refresh the
  purchase-list and badge feature bullets (truth fix above).
- `docs/barcodes.md`: "Closest-item suggestions" section (indicator-stripped
  view, ≥6 significant digits floor, name tie-break, why not the LIKE ladder,
  one keyword dialect, semantic degrade-to-[], misreads never suggested).
- `NOTES.md`: a short paragraph noting the scanner-flows completion.

## Hardware checklist

Write a concrete checklist into the PR description: Track A (HID, executable
now: badge login regression, scan-to-cart incl. UPC-E, countdown
cancel/restart/expiry + close-tab-mid-countdown → token dead, switch with cart
restore + old-token-revoked curl check, intake both roles, staging replay,
pill-masking check, typing-never-hijacked, print-once) and Track B (USB-COM,
**[BLOCKED]** with the pre-flight chain from `SCANNER-AGENT-SYNC.md`: xHCI
remove+rescan, udev rule, ModemManager ignore, power/control=on).

## Gates

`uv run pytest` green; `scripts/ui_check.py --self-host` passes (if a browser
is available); `node --check static/app.js`; README-claims test passes.
No Phase-B relay, no checkout, no JS runner, no CDP interaction layer.
