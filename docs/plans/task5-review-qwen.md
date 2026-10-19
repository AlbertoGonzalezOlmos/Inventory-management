# Task 5 review (qwen3.8-max) — peer proposals for the SPA scan/cart/badge flows

Reviews `task5-proposal-{glm,kimi}.md` against `task5-proposal-qwen.md` and
the merged Task 1–4 backend. All three verified the same ground truth
(including the correction that `ui_check.py` is a one-shot DOM/console
checker, **not** puppeteer — no interaction capability, logged-out surface
only). §4 is the merged recommendation; §5 the changes to my own proposal.

---

## 1. Review of `task5-proposal-glm.md`

### Strengths (adopt)

- **S1 — `[+]` maps to `POST /cart/items {qty:1}`, not PATCH.** The server
  upsert is the race-free increment (SQL-side accumulation, Task 1's whole
  point); client-computed PATCH-set for increments reintroduces read-modify-
  write arithmetic in the browser (two tabs, or a click burst outrunning the
  chain). `[−]` stays PATCH (qty−1, 0 removes). kimi converged; 2-of-3. My
  all-PATCH mapping is withdrawn.
- **S2 — Switch order that needs no `api()` token override**: qr-login
  succeeds (new session *secured server-side* — which is what D3 requires)
  → `logout` **before** `saveSession`, so the wrapper naturally still sends
  the OLD token → `saveSession(new)`. Simpler than my `{token}` option and
  equally D3-conformant ("secured" ≠ "stored locally"). Adopt; my `{token}`
  override is withdrawn.
- **S3 — `err.status`/`err.detail`/`err.data` attached to thrown errors**
  instead of a `{raw}` mode: one code path, every existing catch site
  unaffected (`Error.message` unchanged), scan flows branch on the attached
  structured detail. kimi's R7 independently recommends the same. Adopt
  (replaces my `{raw:true}`).
- **S4 — Queue, not drop**: `scanQueue` + single drain loop + `scanBusy`,
  because a presentation scanner fires faster than a round trip and
  overlapping async `handleScan`s would reorder cart updates and stack
  modals. My proposal left scans implicitly concurrent — a real bug class.
  Adopt (with glm's own risk-9 cap ~10 + "scanner is running ahead" toast).
- **S5 — `loadCart` hooked into `saveSession()`** — every session-entry
  path (password, badge, register, switch) funnels through it, so no call
  site can be forgotten. Adopt (with my `must_change_password` guard inside
  — the cart endpoints 403 under the gate; glm omits this and the flagged
  switch would throw mid-flow).
- **S6 — Logoff overlay: no backdrop-click dismiss, no ✕** — leaving is a
  deliberate act (button, expiry, or a badge decision). Adopt (kimi agrees).
- **S7 — Attribution banner with per-row [Details]** and unknowns *staying
  staged* (no modal ambush chain after login) — least-intrusive replay UX.
  Adopt (softened with kimi's auto-open-first, §2 S4).
- **S8 — Honest ui_check correction + "no test-only hooks in production
  JS"** (`window.hcrmTestScan` backdoors explicitly refused) — matches repo
  doctrine; adopt as a stated rule.

### Weaknesses / risks / bugs (avoid or fix)

- **W1 — The countdown time source claim is inverted.** "remaining =
  deadline − Date.now() … the server timestamp is the truth — client clock
  skew and request latency never touch it": `deadline` is a *server-clock*
  instant and `Date.now()` is the *client* clock, so their difference is
  exactly where skew lands (seconds-to-minutes on a badly-set kiosk; the
  overlay could open at 0 or at 90). Receipt-based counting
  (`endsAt = Date.now() + grace_seconds*1000`, my proposal) has only
  one-way-latency error (~ms). The server deadline remains the *enforcement*
  truth either way (Task 4), so this is UX accuracy, not security — but the
  merged spec must not carry the inverted rationale. Fix: receipt-based
  countdown; display remaining seconds only.
- **W2 — Full stand-down on editable targets is a kiosk-operational hole.**
  `if (inField) { buf=""; return; }` means a scan arriving while ANY input
  is focused is not captured — and focus is *usually* somewhere: the login
  email field has `autofocus`, the catalogue has a search box. The scan then
  silently types garbage into the field and Enter submits it. The shipped
  login-badge flow survives (dedicated field), but "works on every screen"
  (the task's premise) fails on the two most-used screens. glm's own
  rationale ("a capture field would break when a modal steals focus") argues
  against a *field*, not against capturing while inputs are focused. Fix in
  §4: observe-only mode on editable targets (buffer, never preventDefault
  chars; on gated Enter, handleScan + preventDefault the Enter + strip the
  exact suffix from the field with an `input` event so v-model syncs).
- **W3 — No `qr-identify` 403 path.** Task 4 (per spec) did NOT exempt
  identify from the password gate — a flagged kiosk session (staff-created
  account, badge printed at creation, password unchanged: precisely the
  Task-2 counter flow) gets 403 from identify, and glm's flow has no
  branch: the scan dead-ends in a toast. My fallback (qr-login → compare
  user.id → same: revoke the spare + arm logoff; different: continue as
  switch) is required for feature completeness. Adopt into the merged spec.
- **W4 — Legacy-import retry parses the 404 string detail** ("a 404 names
  the missing ones; catch it, drop the named ids, retry once"): the detail
  is a human string (`"Item(s) not found: [3, 7]"`) — parsing ids out of it
  is exactly the brittleness the all-or-nothing PUT review warned about.
  The per-entry POST fallback (mine/kimi's) needs no parsing. Fix.
- **W5 — Auto-generated hidden SKU** (`scan-<gtin14>` / `scan-<slug>`):
  hides a required, unique, permanent identity field from the staff operator
  and manufactures collision-prone slugs; 409s become the norm instead of
  the exception. Editable prefilled SKU (mine + kimi, 2-of-3) keeps staff in
  control of catalogue identity. Reject.
- **W6 — In-memory-only staging** (shared with kimi): a kiosk page reload
  silently discards scans the banner promised were "waiting". Codes are not
  secrets; localStorage persistence is ~5 lines with a cap. See §2 W3 for
  the verdict (2-of-1 against me — I keep recommending persistence but flag
  it as the coordinator's call, downgradable in one commit).
- **W7 — No visible scan field at all** ("there is no capture field to
  focus"): combined with the 50 ms timing gate, a slow USB-HID emulator
  (>50 ms inter-key — kimi's R1 names the class) is *uncapturable anywhere*
  on catalogue screens, and sub-4-char internal codes have no path at all.
  The visible field is the deterministic no-heuristics fallback for both.
  Reject the omission (2-of-3: kimi also drops it — but kimi at least keeps
  the login badge field's coverage argument honest; glm's pill is feedback,
  not input).

### Verdicts vs my proposal

| Topic | glm | mine | Verdict |
|---|---|---|---|
| [+] increment | POST upsert | PATCH set | **glm** (S1) |
| Switch revoke ordering | logout-before-saveSession | saveSession + `{token}` revoke | **glm** (S2) — I withdraw `{token}` |
| Structured errors | err-attachment | `{raw:true}` | **glm/kimi** (S3) — I withdraw raw |
| Scan concurrency | queue + drain | implicit | **glm** (S4) |
| Countdown source | deadline − Date.now() | receipt + grace | **Mine** (glm W1) |
| Capture on focused inputs | stand-down | burst-capture w/ sinks | **Neither as-is** — merged observe-only mode (§4) |
| Staff SKU | auto-generated | editable prefill | **Mine+kimi** (W5) |
| Replay unknowns | banner + [Details] | modal queue | **glm** (S7) + auto-open-first (kimi) |
| Visible scan field | none | kept | **Mine** (W7) |

## 2. Review of `task5-proposal-kimi.md`

### Strengths (adopt)

- **S1 — `_cartOp`/`flushCart` formulation, with `loadCart` after
  `saveSession` routed through the same chain (R4).** Names the save-then-
  switch flush primitive precisely ("draining the queue, not persisting
  state") and closes the post-switch interleave I hadn't covered. Adopt
  verbatim (merged with glm S4's queue for *scans*; two chains or one — one
  chain for cart ops, one for scan drain, is fine, both are 5-liners).
- **S2 — R2's naive-UTC parsing trap** (`Date.parse(deadline)` without the
  `"Z"` append is timezone-wrong): the concrete bug that receipt-based
  counting avoids entirely — kept as the documented reason, plus the note
  that a `Z`-suffixed server response would be a one-line backend nicety if
  the deadline is ever displayed.
- **S3 — R3: the `hcrm-session-expired` handler must clear the logoff timer
  and null the overlay** (or the interval fires `logout` against a dead
  session — harmless but noisy). I had it; kimi states the failure mode
  best. Adopt the wording.
- **S4 — Auto-open the intake modal for the FIRST replayed unknown** (the
  rest stay reachable from the banner): the middle ground between glm's
  pure-banner and my modal-chain. Adopt.
- **S5 — `data-scan-capture="on"` attribute on the app root, set by the
  listener installer** — a positive ui_check pin that the capture bus was
  actually installed (mount-order regressions drop it), using only the
  existing `count()` mechanism, parser untouched. The best automated pin of
  the three proposals. Adopt (alongside my static "Scanner ready" copy pin —
  belt and braces, both one-liners).
- **S6 — Shared `badgePrint` modal generalising `memberBadge`** (the
  rotate/revoke buttons migrate to it): dedup of markup that is already
  duplicated; **SVG-only, payload never held in component state** —
  minimal exposure, consistent with the existing modal. Adopt (I withdraw
  my copyable-payload display; flagged as an open question if the owner
  wants manual-entry fallback — the raw-43 form works, but the SVG is the
  deliverable).
- **S7 — 409-recovery for staff create**: pre-check `GET /items?barcode=`
  AND on a raced 409, fetch the conflicting item and offer "already filed
  as ‹sku› — added instead". Complete handling of both the slow and the
  race path. Adopt (superset of mine).
- **S8 — R6 (intake modal during an armed countdown)**: countdown keeps
  running underneath; expiry logs off mid-modal — "the desired safety
  property". Correct and worth stating in the merged spec.

### Weaknesses / risks / bugs (avoid or fix)

- **W1 — Same editable-target stand-down hole as glm W2** (`if target is
  editable → return`), with the same kiosk-autofocus consequence. Same fix
  (§4 observe-only mode).
- **W2 — No shape gate at all** ("the server owns classification"): true,
  but every scanner-paced Enter-terminated burst then costs a server round
  trip and a user-visible 404/422 toast — including bursts that are plainly
  not codes. The client gate exists to avoid *nonsense calls*, not to
  pre-judge identity (glm's framing, which kimi's own §2.4 half-echoes).
  Keep the routing+shape gate (badge regex | digits 6–14 | conservative
  opaque charset ≥4), server remains the authority for everything else.
- **W3 — In-memory-only staging** (R5): same as glm W6. Two peers vs me —
  but the failure mode (banner says "N waiting", reload silently drops them
  at the exact feature whose job is not losing unattributed scans) argues
  for the 5-line localStorage version. I keep recommending persistence;
  coordinator's call, one-commit downgrade either way.
- **W4 — Staff ScanRequest queue UI deferred** (Q2): leaves member filings
  write-only — no screen to see or resolve them; the feature's back half
  (Task 3's resolve/dismiss endpoints, `add_to_cart`) stays API-only.
  glm + I include the tab (2-of-3). Include in increment 5c, with the
  queue's `add_to_cart` checkbox **default-on** (kimi's own Q2
  recommendation + glm's — "the member scanned it wanting it"; the server
  default stays false, the *staff UI* opts in deliberately).
- **W5 — No flagged-session identify fallback** (same as glm W3): §3b has
  no 403 branch. Required (my fallback, §4).
- **W6 — Countdown from server deadline** (`Date.parse(deadline+"Z") −
  Date.now()`) — skew-exposed like glm's (their R2 mitigates the parse trap
  but not the clock-offset error). Receipt-based counting, §4.
- **W7 — No visible scan field** (explicit reversal of their phase-2
  position): the reversal argument ("focus rule closes the false-positive
  surface") is sound *for false positives* but ignores the false-*negative*
  side — slow emulators (their own R1!) and short opaque codes then have no
  path on catalogue screens. Keep the field (§4).

### Verdicts vs my proposal

| Topic | kimi | mine | Verdict |
|---|---|---|---|
| Mutation queue + flush naming | `_cartOp`/`flushCart`, loadCart chained | `_enqueueCart` | **kimi** (S1) — same idea, better scope |
| Shape gate | none | two-class gate | **Mine** (W2) |
| Replay unknowns | auto-open first + re-stage | modal queue for all | **Merge**: glm banner + kimi auto-first |
| Badge modal | shared `badgePrint`, SVG-only | `badgeOnce` with payload text | **kimi** (S6) |
| 409 staff-create recovery | pre-check + raced-409 fetch | pre-check only | **kimi** (S7) |
| Queue UI | deferred | included | **Mine+glm** (W4) |
| Staging persistence | in-memory | localStorage | **Mine** (contested 1-vs-2; flagged) |
| ui_check pin | installer attribute | static copy pin | **Both** (S5) |

## 3. Three-way convergence (fast-path)

`cart` state replacing `purchase` (line shape identical — template mostly a
rename); watcher deleted; mutators return the full `CartOut` and assign it;
stock warns, never blocks (decision 6); one-time legacy import gated on an
empty server cart, key removed on first contact; PUT stays the import
primitive (replay uses per-code `POST /cart/scan`); document-level
capture-phase keydown listener with 50 ms timing gate + Enter terminator +
login-badge-field non-regression; `handleScan` as the one bus with the
badge/barcode prefix route; identify → schedule → single non-stackable
overlay, restart-on-same-badge, cancel-and-switch on different badge, one
interval with disciplined teardown (cancel/expiry/switch/route/
session-expired); switch order flush → qr-login (abort on failure, stay
put) → revoke old → saveSession → load, `must_change_password` branch
reused, `busy` guard on the whole flow; intake modal with classification
header, verbatim server `message` (incl. UPC-E hint), clickable suggestion
groups, live debounced `/items/suggest` as the name is typed (where
`by_semantic` appears, degrade-safe), role-aware submit (staff create +
add; member ScanRequest with 409-already/duplicate handling); attribution =
badge-primary + staged replay, no member picker, no new endpoints;
one-shot badge modal on register/member-create, print-isolated via the
repo's first `@media print` rules, payload never persisted; no JS test
runner, no test-only production hooks; ui_check stays DOM/console-gated
(parser untouched); backend pytest suite is the semantics owner and stays
green with zero backend changes; four-ish landing slices each leaving the
SPA loadable.

## 4. Final recommended shape (merged deltas over §3)

1. **Capture (the one genuinely merged design).** Document-level,
   capture-phase, always on; three target modes:
   - `[data-scan-sink]` targets (login badge field, visible scan field):
     full stand-down — their own Enter handlers own them.
   - **Editable targets (input/textarea/select/contenteditable): observe-
     only.** Buffer under the timing rules but *never* preventDefault
     character keys (human typing and auto-repeat are untouchable); on
     Enter, if the buffer passes the routing+shape gate → preventDefault the
     Enter (stop form submits), `handleScan(code)`, and strip the exact
     suffix from the field (`el.value.endsWith(code)` → slice +
     `dispatchEvent(new Event("input"))` for v-model sync — strip only on
     exact match, never a guess). Gate fails → nothing happens, Enter keeps
     its default. This closes glm/kimi's focused-input hole *and* my
     swallowed-keystroke flaw.
   - Non-editable targets: full mode — preventDefault chars from the second
     fast key onward, Enter on gate pass.
   Timing: <50 ms inter-key; aborts on slow key, modifier, or non-printable
   (Enter terminates). Gate: badge `^HCRM1:[A-Za-z0-9_-]{43}$` or bare-43;
   barcode digits 6–14 or `[A-Za-z0-9._/-]{4,64}` (min length 4 per glm).
   **Visible scan field kept** (catalogue header/counter strip): no gate,
   operator intent, covers slow emulators and short codes. **Scan queue +
   drain loop** (glm S4), cap ~10 + warning toast; **fixed pill flash** for
   visibility (glm) + outcome toasts.
2. **Modal discipline**: any non-logoff modal open → barcode scans dropped
   with a hint toast, badge scans still processed; logoff overlay keeps the
   listener live; overlay has no ✕/backdrop dismiss; countdown computed
   **from receipt + `grace_seconds`** (server deadline = enforcement truth;
   display never subtracts a server timestamp from a client clock — glm W1
   correction); interval teardown on cancel/expiry/switch/route change/
   `hcrm-session-expired` (which also nulls the overlay, kimi R3); expiry
   mid-intake-modal logs off anyway (kimi R6 — desired).
3. **Badge-logged-in flow**: `qr-identify`; **403 (flagged session) →
   fallback**: `qr-login` → compare `user.id` → same: `logout` the minted
   spare immediately, then arm the overlay; different: proceed as switch
   from step 2. 401/network → global handler already routed to login; re-run
   as logged-out badge flow. (glm W3 / kimi W5 fix.)
4. **Switch flow (glm S2 order)**: `busy` guard → `await flushCart()` →
   best-effort `logoff-cancel` if armed → `qr-login` (**abort on failure,
   stay logged in, toast**) → `logout` (old token still in localStorage;
   try/catch; a 401 there means it was already dead — the wrapper's state
   clear is immediately overwritten by the next step, harmless) →
   `saveSession(new)` → `loadCart` **through the cart chain** (kimi R4;
   skipped when flagged) → `must_change_password` branch → reload
   catalogue → "Switched to ‹name› — ‹old›'s list was saved" toast →
   `replayStagedScans()`.
5. **`api()` changes**: attach `err.status` / `err.detail` / `err.data` to
   thrown errors (glm S3 / kimi R7); **no `{token}` option, no `{raw}`
   mode** (both withdrawn). 401 whitelist untouched.
6. **Cart**: `[+]`/add → `POST /cart/items`; `[−]` → PATCH qty−1 (0
   removes); remove/clear → DELETEs; all via `_cartOp` chain;
   `flushCart()` = drain; `loadCart` inside `saveSession()` (glm S5) with
   the flagged guard; legacy import: PUT-first, **per-entry POST fallback
   on 404 (no detail-string parsing — glm W4 fix)**, key removed on first
   contact, imports only into an empty server cart.
7. **Attribution**: staged codes in component state **mirrored to
   localStorage (cap 50)** — recommended, flagged as the contested point
   (glm/kimi prefer memory-only; one-commit downgrade); banner on
   login/register with per-row **[Details]** (glm S7); replay after any
   login: sequential `cart/scan`; added → toast (summarised); misread →
   toast + drop; unknown → stay staged, **auto-open the intake modal for
   the first** (kimi S4), rest via banner.
8. **Intake modal**: classification header + verbatim message; grouped
   suggestions (family / code-name), click = add-that; live `/items/suggest`
   (debounced) adds the semantic group; staff variant = **editable SKU
   prefilled `gtin14 || code`** + pre-check `GET /items?barcode=` + raced-
   409 fetch-and-offer (kimi S7) + create → add → best-effort resolve of a
   matching open request; member variant = ScanRequest with 409 handling;
   embeddings-503 surfaces the server's message.
9. **Staff "Scan requests" admin tab** (included, 2-of-3): status filter,
   [Create & resolve] (prefilled item form; `add_to_cart` checkbox
   **default-on** in this UI — kimi Q2/glm), [Dismiss]. **Account view:
   "My scan requests"** (`/mine`, status chips) — kept, flagged droppable.
10. **Badge print**: shared `badgePrint` modal generalising `memberBadge`
    (kimi S6), **SVG-only** (payload never in state/storage), "shown only
    once" warning, `window.print()` + the new `@media print` isolation
    (`printing-badge` body class).
11. **ui_check gate**: existing pins stay; add kimi's installer-set
    `data-scan-capture="on"` root attribute pin + my static "Scanner ready"
    login-copy pin + badge-field placeholder pin; zero console errors with
    the listener installed; `count()`-mechanism only, parser untouched.
12. **Slices** (coordinator's incremental landing): 5a cart-on-API (+api()
    err-attachment, import, saveSession funnel) · 5b capture + queue + badge
    flows (overlay, switch, flagged fallback) · 5c barcode flows (scan,
    intake, staging/replay, Requests tab, /mine) + badge print + print CSS +
    ui_check pins. Each slice leaves the SPA loadable and ui_check green.

## 5. Changes to my own proposal (self-review)

1. **Withdraw `{token}` and `{raw:true}` api() options** → err-attachment
   (glm S3) + logout-before-saveSession ordering (glm S2). Simpler, same
   guarantees.
2. **Withdraw PATCH-based `[+]`** → POST-upsert increment (glm S1/kimi).
3. **Fix my capture design's editable-target flaw**: preventDefault-from-
   second-fast-key would swallow fast human typing and auto-repeat inside
   inputs → replaced by the observe-only + gated-Enter-strip mode (§4.1),
   which also fixes glm/kimi's opposite hole (no capture while focused).
4. **Withdraw the replay modal-chain** → banner + [Details] + auto-open-
   first (glm S7 + kimi S4).
5. **Withdraw payload-text display in the badge modal** → SVG-only shared
   `badgePrint` (kimi S6).
6. **Adopt** `loadCart`-in-`saveSession` funnel (glm S5, with my flagged
   guard), `_cartOp/flushCart` naming and post-switch chaining (kimi S1/R4),
   scan queue + drain + pill (glm S4), kimi's 409 raced-fetch recovery
   (S7), installer-attribute ui_check pin (S5), opaque min-length 4 (glm),
   queue-UI `add_to_cart` default-on (glm/kimi).
7. **Kept against both peers** (with reasons, not stubs): receipt-based
   countdown (glm W1 — their skew claim is inverted); shape gate (kimi W2);
   visible scan field (glm W7/kimi W7 — slow emulators, short codes);
   flagged-403 identify fallback (both W3/W5 — the feature is broken
   without it for exactly the Task-2 counter-issued accounts); editable SKU
   prefill (glm W5); no 404-detail-string parsing (glm W4); localStorage
   staging persistence (contested 1-vs-2 — flagged for the coordinator,
   one-commit downgrade).

## 6. Verdict

The three proposals agree on the architecture and differ on four
mechanisms, and on each, at least one proposal had a real defect: glm's
countdown rationale is inverted (W1) and its import retry parses a human
string (W4); kimi has no shape gate (W2) and defers the staff half of the
ScanRequest lifecycle (W4); mine had the keystroke-swallowing flaw inside
editable fields and an over-intrusive replay chain; glm and kimi *both*
leave scans uncaptured whenever an input is focused (the kiosk's normal
state) and *both* lack the flagged-session identify fallback. The merged
shape in §4 takes the best of each and is the one I recommend the
coordinator standardise on. Open choices left to the coordinator: staging
persistence (localStorage recommended, memory-only acceptable), the
`/mine` account list (keep/drop), SKU prefill convention (kimi Q1 —
recommend `gtin14 || code`, editable), and whether a CDP-driven
interaction layer for ui_check becomes a follow-up task (glm risk 2 —
recommend yes, later, not a rider on Task 5).
