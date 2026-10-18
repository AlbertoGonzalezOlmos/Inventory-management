# Coordinator synthesis & prioritized plan

Merged from `plan-glm.md`, `plan-kimi.md`, `plan-qwen.md` (all phase-2 reconciled).
Derived against repo HEAD `bfd09fe` (2026-10-11).

## What the three plans agree on (do not rebuild / already exists)

- M-10 driver stack (`app/scanner/m10.py`, `protocol.py`, `transport.py`) — works.
- QR-badge credential system (`app/qrbadge.py`) — hash-at-rest, SVG, issue/rotate/revoke, login-page badge field.
- QR login endpoint (`POST /api/auth/qr-login`) and GTIN normalisation/identity (`app/barcodes.py`, `analyze_scan`, `GET /api/items?barcode=`).
- Bridge (`scripts/scanner_bridge.py`) with exact-identity lookup + badge→qr-login exchange.

## What is missing (the actual gaps)

1. No server-side per-user cart — the purchase list is browser localStorage, user-agnostic (even leaks across users on logout). This makes "save then switch" meaningless.
2. No auto-generated QR badge at account creation (`register()` / `create_member()`).
3. No way for scans to reach the UI outside the login field (no global capture / relay).
4. No scan-to-cart endpoint, no unknown-barcode intake (name+price + suggestions), no attribution flow.
5. No 30-second logoff countdown, no cross-account save-then-switch semantics.

## Coordinator decisions (resolving the plans' open questions)

1. **Member item creation (D4/R6):** staff-only catalogue creation. Unknown items scanned by members become a **ScanRequest** for staff to resolve. No member quick-create. (All three converge.)
2. **Attribution (D5):** badge-scan is the primary "which account?" answer; staff can assign via a secondary path. Anonymous scans are staged and replayed after login. No anonymous member picker.
3. **Switch token handling (D7):** revoke the old kiosk token (`POST /api/auth/logout` with the old token) **only after** the new session is secured.
4. **30-second logoff (D6):** client-side countdown for UX **plus** server-enforced lazy expiry (`logoff_deadline` on the auth token, checked in `get_current_user`), so a dead browser still logs the counter off.
5. **Relay scope:** USB-HID global capture ships first (matches the hardware reality — dev unit is HID personality A001; USB-COM unverified due to the dead xHCI controller). The USB-COM counter-session/relay (Phase B) is **deferred** to a later point in the plan.
6. **Cart vs stock:** adding to cart never mutates stock. UI warns when qty > stock (no hard clamp by default).
7. **Suggestions ("closest items via the barcode system"):** GS1 company-prefix siblings + keyword tokens; semantic vector search degrades to empty on 503.
8. **Scan resolution:** a single shared decision core (`app/scanlookup.py`, extracted from the bridge's tested `find_item`) so the bridge and server never drift on identity.

## Prioritized plan (order of execution)

1. **Task 1 — Server-side per-user cart** (foundation). `CartItem` model + `/api/cart` CRUD, per-user isolation, cleanup on item/user delete, migration, concurrency test.
2. **Task 2 — Auto QR badge at account creation.** `register()` / `create_member()` generate a badge, returned once (printable SVG); seeds excluded; tests.
3. **Task 3 — Scan resolution + `/api/cart/scan` + suggestions + member ScanRequest** (backend). Shared `app/scanlookup.py`; 404 with structured detail + prefix/keyword suggestions; `GET /api/items/suggest` with degradation.
4. **Task 4 — Server-enforced logoff deadline** (small auth addition) + same-user `qr-identify` check.
5. **Task 5 — SPA flows (the heart).** Cart on the API (legacy localStorage one-time import), global HID scan capture, `handleScan()` bus: badge flows (own badge → 30s logoff countdown with cancel/restart; other badge → save-then-switch) and barcode flows (add-to-cart; unknown → name+price modal with suggestions; no-user → attribution), one-shot badge print.
6. **Task 6 — Docs + security review + migrations check + hardware checklist.**
7. **(Deferred) Phase B — USB-COM counter sessions + bridge relay.**

## PR slicing

Each task lands as its own PR with `uv run pytest` green. Task 5 lands incrementally (cart UI first, then capture+flows, then modals).
