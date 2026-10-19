# Round 3 cross-review — qwen3.8-max

Reviewed in full: `docs/plans/r3plan-glm.md` (rev 2) and
`docs/plans/r3plan-kimi.md` (rev 2). Both had already cross-reviewed my rev 1
and adopted most of it, so the three plans are ~85 % converged. That changes
what a useful review is: not "whose architecture wins" but **(a) which residual
differences are real, (b) which shared claims are factually wrong, and (c) what
all three missed.** I re-verified every contested claim against the tree, the
vendored Vue bundle, and the host — the measurements are below, and they change
three conclusions.

Verdict summary: **glm's rev 2 is the strongest of the three** (it caught a real
hole in my state machine and one copy site I missed); **kimi's rev 2 is the most
complete on guards** (it is the only one that kept my stale-snapshot rule *and*
the paste/cut/drop flushes); **both share two factual errors** that I also held
in rev 1, and **both rest T3's verification on a false premise about the
hardware**. My own rev-1 defect ledger is §3 — it has five entries, one of them
a correctness bug in the design I was defending.

---

## 1. Convergence (already settled — do not re-litigate)

All three plans independently landed on the same shape. Recording it so the
coordinator can treat these as decided unless someone objects:

| Topic | Agreed design |
|---|---|
| Task order | T0 harness/pure-move → T1 grace 60 → T2 calendar endpoint → T3 capture → T4 greeting → T5 review screen → T6 calendar UI → T7 docs. Frontend tasks strictly serial (same three files); T0/T1/T2 parallel. |
| Testability | Extract pure modules (`static/scan-capture.js`, `static/purchase-calendar.js`), dual-mode export (`module.exports` / `window.*`), classic `<script src>` before `/app.js`, `node --test tests/js/` in a **new CI `js` job**; `pytest` stays Node-free; a missing toolchain fails, never skips. |
| Item 1 mechanism | Defer the burst's first key (`SCAN_DEFER_MS = 60 > SCAN_MAX_GAP_MS = 50`), replay-on-proof-of-human, retract-on-burst-confirm, per-type target classification, WARM fast-path, `SCAN_STRICT_DEFER` escape hatch, inequality pinned in Node **and** pytest. |
| Item 2 | One constant (`app/security.py:166-168`) + a full copy sweep + `test_default_grace_is_60` + a contract pin tying the template copy to the parsed default. No SPA logic change. |
| Item 3 | Banner (not a toast), set in `saveSession`, **not** cleared in `_closeOverlays()`, cleared in `logout`/`handleSessionExpired`. No routing change needed. |
| Item 4 | `#/checkout` route reusing the *same* catalogue `<section>` (one grid, one `#sentinel`), server cart as the only state, `_cartOp`-serialized mutations, no `confirm()`, receipt modal exception-only. No backend change. |
| Item 5 | `GET /api/orders/calendar?year&month`, UTC-day bucketing server-side, `{days: {…:{cents,count}}, total_cents, purchase_count}`; gradient client-side in HSL (`t=0` ⇒ CSS `lightgreen`, which makes "first purchase is light green" free). |

---

## 2. Per-peer review

### 2.1 glm (`r3plan-glm.md`, rev 2)

**Strengths to adopt**

1. **The BURST-state timer re-arm — a real bug in my rev-1 diagram.** My §3.1
   armed the defer timer only on entering HELD and never said what happens to it
   in BURST. Both readings are bad: if the timer survives into BURST it fires
   ~60 ms after key #1 — i.e. **mid-payload**, flushing the half-typed code into
   the field and defeating the entire requirement; if it is cancelled at
   confirmation, a burst whose source dies (scanner unplugged, CR suffix
   disabled) hangs with its characters held indefinitely and they reappear at
   some arbitrary later `flush()` trigger (a click 30 s later). glm's fix —
   **re-arm on every held key** — is correct and safe exactly for the reason it
   gives: `60 > 50 > 10-20 ms`, so a live burst can never let the timer elapse,
   while a dead source flushes 60 ms after its last key. **Adopted.**
2. **`app/routers/auth.py:271`** — a second "now+30" copy site (`"the SPA counts
   down from THIS timestamp, never from a client-side now+30"`). Verified by
   grep; my rev-1 T1 listed only `auth.py:266` + `schemas.py:154`. **Adopted.**
   (Also worth recording what *not* to touch: `app/routers/items.py:466` says
   "30 s pool timeout" — unrelated, and a blind sweep would corrupt it.)
3. **Risk 4's framing of the review-screen alternative** (catalogue+panel vs a
   grid-free screen needing a search-to-add box) is a better statement of the
   trade than mine. Adopted as a named decision.
4. glm's T5 keeps **always-a-toast** on success, with a distinct message when
   lines were skipped — right, and it pairs correctly with the exception-only
   modal. Adopted verbatim.

**Weaknesses / risks**

* **Risk 9 is factually wrong** and would encode the error in a test:
  *"`v-model.number` fields (admin price/stock) listen to `change` — number
  inputs are never held; pinned as an explicit test."* Decoded from the vendored
  bundle (`static/vendor/vue.global.prod.js`, `vModelText = lK`):
  `lx(e, t?"change":"input", …)` where **`t` is the `lazy` modifier**, and
  `l = number || props.type === "number"` only selects a `looseToNumber`
  coercion. So `.number` listens to **`input`**, and `type="number"` is coerced
  automatically. A test asserting the `change` premise would be testing a
  falsehood; the *conclusion* (never hold number inputs) is still right, for a
  different reason — see §4 C1.
* **Missing the stale-snapshot guard.** glm's flush trigger list is
  `pointerdown`/`focusin` only. A replay lands up to 60 ms after the snapshot,
  so a mouse paste / autofill / drop in that window is spliced over by the
  replay. kimi kept my rule; glm dropped it. Real, if rare, and one line to fix.
* **No mention of the sentinel-prefix follow-up** (the only thing that removes
  the heuristic entirely for our own hardware). kimi and I both carry it.
* glm's grounding still repeats "the M-10 is dead" — see §4 C4.

**Disagreements**

| Point | glm | qwen | Better, because |
|---|---|---|---|
| Replay event type | `InputEvent("input",{bubbles})` | plain `Event("input",{bubbles})` | **qwen** (trivially): it is what the shipped restore already dispatches (`app.js:966`), and it keeps one global out of the Node harness. `InputEvent` buys nothing here — Vue reads `target.value`. |
| Number inputs | never held, rationale = `v-model.number` → `change` | never held, rationale = unreadable caret | **qwen's rationale, glm's conclusion.** The premise is false (§4 C1); the decision is the same. |
| Hardware evidence | simulator only | re-plug first, simulator as fallback | **qwen** — measured (§4 C4). |

### 2.2 kimi (`r3plan-kimi.md`, rev 2)

**Strengths to adopt**

1. **The most complete guard list of the three**: `repeat`, `isComposing`/229,
   modifiers, non-printable-flush-first, `pointerdown`, `focusin`, **and**
   `paste`/`cut`/`drop`, plus the **stale-snapshot rule** ("if `el.value`
   changed since the snapshot, insert at the CURRENT caret"). Adopted as the
   canonical list.
2. **The re-arm is annotated in the diagram itself** (not just prose), with the
   reason inline. That is where an implementer will look. Adopted.
3. kimi states the **UTC-day rationale** in the form that actually decides it:
   "`formatDate` renders naive-UTC as local wall-clock, so UTC-day dots always
   agree with the list rows" — and it explicitly retracts its own rev-1
   local-day bucketing. That retraction is the right call and the clearest
   statement of why.
4. **Risk 7 (native undo)**: splices are invisible to the browser's undo stack;
   Ctrl+Z after a replayed scan may jump oddly. Accepted + documented in the
   module docstring. Adopted.
5. kimi's T3 test list is the most concrete ("one preventDefault per run",
   "zero held chars at end, exact sequence"), which is what makes ~30 cases
   reviewable rather than aspirational. Adopted as the matrix baseline.

**Weaknesses / risks**

* **Inherits the same `v-model.number → change` error** (T3 classification).
  Same correction as glm.
* **Inherits the "M-10 dead" premise** and strengthens it into a design
  constraint ("synthetic-event simulation is the only capture-verification
  path, so the handler must never check `isTrusted`"). The `isTrusted`
  conclusion is right; the premise is not (§4 C4), and the non-sequitur matters:
  a *hardware* pass also requires no `isTrusted` check, so the constraint does
  not depend on the hardware being absent.
* **No `days`-key-format test rationale** (it lists the regex test but not why:
  the SPA indexes the map with a locally built key, so a format drift is a
  *silent* empty calendar). Minor.
* kimi's T5 table repeats "Deliberately a NON-held capture target" for the qty
  box without the reason; with the corrected rationale (§4 C1) the row becomes
  reviewable.

**Disagreements**

| Point | kimi | qwen | Better, because |
|---|---|---|---|
| Month navigation | "optional, coordinator call" | ship it (endpoint makes it ~10 lines) | **Tie / coordinator.** Both defaults are defensible; the endpoint exists either way. I keep "ship, trim on request". |
| Exception taxonomy for WARM | one bounded exception (1 char/1 frame) | two: E1 bounded (WARM-only) + **E2 unbounded but pre-existing and mode-independent** | **qwen** — kimi's (and my rev-1's) single-exception claim is incomplete; see §3 L3. |
| Credential fields | treated like any text input | `SCAN_NO_WARM_SELECTOR` — never WARM for `type=password` / `autocomplete=username` | **qwen** (§5 D1c): it is the only way to make "no part of a badge payload can ever paint" literally true, for ~5 lines. |

---

## 3. My own rev-1 defect ledger (honest, all five)

* **L1 — the BURST-state timer hole (glm's catch).** My §3.1 diagram had no
  `BURST ──timer──▶` transition. As analysed in §2.1.1, either reading is a
  defect (mid-payload flush, or an indefinitely hanging hold). **Fixed**: re-arm
  on every held key; `BURST ──timer fires──▶ replay → IDLE` (IDLE, *not* WARM —
  sub-50 ms arrivals are not proof of a human, and setting WARM there would
  widen E1 for the next 400 ms).
* **L2 — I flip-flopped on `type=number` and landed wrong in rev 2.** Rev 1 said
  "never held, value-only retract"; then last turn I changed it to "held with an
  append-style replay" to make "never appears" uniform. That is worse: the caret
  is unreadable on a number input, so an append-replay of a *human's* deferred
  character inserts at the end regardless of where they clicked — silently
  turning `12.|50` + `9` into `12.509`. Corrupting staff input is worse than a
  ≤1-frame digit flash. **Reverted to "never held"**, with the corrected
  rationale (§4 C1).
* **L3 — I conflated two different exceptions.** My rev-1 (like both peers')
  described *one* bounded WARM exception. There are two, and the second is not
  bounded:
  * **E1 (WARM-only, bounded):** the scan's first key arrives **≥50 ms** after a
    human keystroke but inside the 400 ms warm window → the snapshot exists → 1
    character paints for ≤1 frame, then is retracted. Strict mode: 0 characters.
  * **E2 (inherent, both modes, unchanged from today):** the scan's first key
    arrives **<50 ms** after a human keystroke in the same field → there is no
    burst boundary to detect; the payload accumulates into `_buf` *mixed with the
    human's characters*, every char lands natively, and the combined string fails
    the shape gate at Enter → the payload stays in the field and is not routed.
    **Strict mode does not help** (the mixed buffer fails the gate there too), so
    E2 is not a cost of WARM — it is the price of timing-based detection, and it
    is exactly today's behaviour. Only a sentinel prefix (§3.6 of the plan) or
    the visible scan field removes it.
  Stating E2 explicitly matters because it bounds what `SCAN_STRICT_DEFER` buys:
  flipping it to `true` closes E1 and **nothing else**. If the coordinator's
  reading of "never appears" is absolute, the honest answer is "then we need the
  sentinel prefix", not "then set STRICT".
* **L4 — missed copy site** `auth.py:271` (glm). **Fixed.**
* **L5 — the greeting's a11y pattern.** I specified `<div class="greeting"
  v-if="greeting" role="status">`. An `aria-live`/`role=status` region that is
  *inserted* with its text already present is announced inconsistently across
  screen readers; the reliable pattern is a **persistent** live region whose
  *text* is conditional. **Fixed**: `<div class="greeting" aria-live="polite">`
  always rendered (visually collapsed when empty), `<span v-if="greeting">`
  inside.

---

## 4. Corrections to shared claims (all measured, not argued)

**C1 — `v-model.number` listens to `input`, not `change`.** Decoded from
`static/vendor/vue.global.prod.js`: `vModelText = lK`, whose `created` hook does
`lx(e, t ? "change" : "input", handler)` with `t` = `modifiers.lazy`, and
`l = modifiers.number || props.type === "number"` selects a `looseToNumber`
coercion of the value. Verified corollaries: (i) there is **no** `.lazy` in
`index.html` (0 matches) — so every text-ish field in the SPA updates on
`input`, and a synthetic `input` is sufficient for replay/retract;
(ii) `type="number"` fields are coerced even without `.number`;
(iii) the handler begins `if (t.target.composing) return` — see C5.
**The correct reason never to hold a number input** is that
`selectionStart/End/setSelectionRange` throw `InvalidStateError` there, so a
replay cannot restore the caret and must append — which corrupts human input
(L2). Decision unchanged; premise replaced. glm's risk 9 and kimi's
classification rationale should both be corrected before implementation, or the
implementer will "fix" the classification on a false premise.

**C2 — the SELECT defect is worse than any of us wrote, and `change` is
mandatory.** From the same bundle: `vModelSelect = lX` binds **`change`** and
computes the model from `Array.prototype.filter.call(e.options, o => o.selected)`
— it never reads `event.target.value`. Native select type-ahead fires `change`
per keystroke, so during a burst into the focused category `<select>`
(`index.html:155`) the model is updated *repeatedly*, and the `category`
watcher (`app.js:313-315`) fires `reloadItems()` **once per scanned character**.
Today's restore then assigns `select.value = "HCRM1:…"` (no matching option ⇒
`""` ⇒ "All categories") and dispatches **`input`**, which `vModelSelect` does
not listen to — so the DOM and the Vue model are left *disagreeing*. The fix is
therefore two-part and both parts are required: restore `selectedIndex` **and**
dispatch `change`. (All three plans say `change`; only this reading explains why
`input` is not merely suboptimal but actively wrong.)

**C3 — use plain `new Event("input", {bubbles:true})`** in replay/retract: it is
what the shipped code already dispatches (`app.js:966`), and `InputEvent` would
be one more global for the Node harness to stub.

**C4 — the hardware premise is wrong; measured on this host just now.**
Both peers write "the M-10 is dead → the simulator is the only capture-
verification path". Measurements:
* `lsusb`: **no `065a` device on any bus** (present: root hubs, a Genesys hub,
  USI `10ab:9309`, Chicony camera). The scanner is simply **not attached** —
  which is a different claim from "the controller is dead". `docs/m10-unreachable-2026-10-10.md`
  §8 records the controller as **revived** (unbind/bind + PCI rescan, no reboot),
  and `usb1` currently enumerates a hub + a device, i.e. that bus is alive.
* `/dev/hidraw0` is the **Synaptics touchpad** (`06CB:CE67`, `hid-multitouch`),
  not a scanner — so the "root-only hidraw" blocker in the sync doc is not
  visible here at all.
* `scripts/udev/99-opticon-scanner.rules` **is installed** in
  `/etc/udev/rules.d/` (dated Oct 10 23:02) and this user **is** in `plugdev`
  and `dialout` (`id`). The rule takes effect on the next attach, so the
  HIDAPI path (`scripts/scanner_hid.py`, `scripts/m10_scan_test.py`) should work
  unprivileged after a re-plug — the sync doc's "one blocker remains" is stale.
* **Decisive:** the sync doc's own last entry shows the wedge personality as
  `/dev/input/by-id/usb-Opticon_Opticon_USB_Barcode_Reader-event-kbd → event14`
  and states "Test it in five seconds, **no sudo, no udev rule**: click into any
  text field … the digits will be typed followed by CR." Root-only
  `/dev/input/event14` blocks *our process* from reading evdev; it does not block
  the X server, which is what delivers keystrokes to a focused browser window.
  **Item 1 is about the wedge path, so hardware verification never needed any
  privilege work.**
Consequence for T3's gate: **primary evidence = re-plug the M-10 and run the
browser pass** (scan a badge on the login screen, a GTIN into the catalogue with
the search field focused, and one into the review screen's qty box);
**fallback = the synthetic-event simulator**. The `isTrusted` rule stands
independently: the Node suite and any console-dispatched simulation require it,
and a hardware pass does not conflict with it. A PR that says "hardware dead"
when the device is merely unplugged would under-deliver evidence we can get.

**C5 — Vue ignores `input` events during composition.** `vModelText`'s handler
starts `if (t.target.composing) return`, and the directive installs its own
`compositionstart`/`compositionend` listeners setting `el.composing`. So a
replay that fires *while a composition is open* updates the DOM value but **not**
the Vue model — a silent desync that no test in any of the three matrices
covers. Fix: flush on **`compositionstart`** (a document-level listener), not
only on `keydown` with `e.isComposing` — the keydown of the first composing key
can arrive before `isComposing` is set on some platforms. **New test case.**

**C6 — T2's range query is safe, and here is the proof nobody wrote.**
`purchases.created_at` has exactly **one** writer in the repo
(`app/routers/orders.py:160`, a raw `INSERT` with
`strftime("%Y-%m-%d %H:%M:%S.%f")`), and that format is byte-identical to
SQLAlchemy's SQLite `DATETIME` bind format — so every stored value is
lexicographically ordered == chronologically ordered, and a bound-`datetime`
half-open range compare is correct. Verified: no `Purchase(...)` construction
anywhere in `app/`, `scripts/` or `tests/` (grep), no migration touching
`purchases` (`app/database.py` has none). Worth writing into T2 as the reason
the ORM range query needs no `substr()` hack — a reviewer will ask.

**C7 — item 1 is a credential-exposure fix, not a cosmetic one (all three plans
understate this).** `index.html:55-56`: the login email input is
`type="text"` **with `autofocus`**. So on the login screen — the single most
scanned screen in a kiosk — a badge scan types the full 49-character
`HCRM1:…` **bearer credential in plaintext into a visible field** for the
burst's duration (~300-500 ms), and `loginForm.email` holds it in the Vue model
meanwhile (available to any password-manager/autofill extension observing the
field). That directly contradicts the doctrine written 600 lines below it in the
same file: `enqueueScan` (`app.js:1027-1031`) — *"NEVER render the raw badge
payload on screen: it is a bearer credential, shoulder-surfable for the pill's
2 s lifetime"* — and masks badges in the pill while the field beside it paints
them. Item 1 closes that contradiction; T3's PR description should lead with it,
and it is the argument that settles Q1 (strict-vs-WARM) for credential fields
specifically → §5 D1c.

**C8 — no shortcuts to break.** `index.html` contains exactly **one** keyboard
handler (`@keydown.enter.prevent="scanFieldSubmit"`, line 136 — itself a sink).
So `preventDefault` on printable keys in a non-editable context cannot break any
app shortcut. Verified, and worth one line in T3 (it is the first question a
reviewer asks about a global capture listener).

---

## 5. New refinements this review adds (none of the three plans has them)

**D1c — `SCAN_NO_WARM_SELECTOR`: credential fields never go WARM.**
`input[type=password], input[autocomplete="username"], input[autocomplete*="password"]`
are always strictly deferred (hold → replay on proof-of-human). Cost: a uniform
≤60 ms lag on the first character of a username/password field (invisible in a
password field — they are dots). Benefit: **E1 is eliminated for the only fields
where a painted character is a credential fragment**, so the design can claim
"no part of a badge payload ever paints on the login screen" without a caveat.
~5 lines + 2 tests. Recommended: adopt.

**D7 — `badgeLogin`'s silent `busy` drop becomes a toast.**
`app.js:417-419`: `if (this.busy) return;` — a badge scanned while any request
is in flight is dropped with **no feedback at all** (the pill fires, then
nothing happens). T5 lengthens that window (the checkout POST holds `busy`), so
an operator scanning a badge during a purchase gets silence. One line:
`if (this.busy) { this.showToast("One moment — finishing the previous action"); return; }`.
Keep the guard itself (it prevents minting a throwaway session — its comment
says why).

**D8 — the review screen's qty box will visibly snap to server truth.**
`:value="entry.qty"` + `@change` (not `v-model`) means a concurrent scan or a
rejected 422 re-renders the input from `this.cart`. That is correct (server
truth wins) but should be stated, with the revert path (`toast + loadCart()`),
so nobody "fixes" it into a `v-model` that fights the `_cartOp` chain.

**D9 — a scan routed while a modal is open: verified *unchanged*, not a
regression.** I checked this because the new design looks like it loses the
payload where the old one recovered it. It does not: today's restore assigns the
**pre-burst** value (`app.js:964-967`), i.e. it also removes the payload before
`enqueueScan`. So "Finish the current dialog first" (`app.js:1089-1092`) drops
the code in both designs; the pill still shows a barcode for 2 s, and the
visible scan field remains the deterministic re-entry path. No action — but it
belongs in the PR so a reviewer does not have to redo the check.

**D10 — unshaped Enter into the login field still submits the form.**
In the replay path (shape gate fails) Enter keeps its default ⇒ implicit
submission ⇒ `doLogin` with the replayed text as the email ⇒ a 401. Identical to
today (the model is updated synchronously by our `input` dispatch before the
`submit` event runs). Add it as an explicit test case so the behaviour is pinned
rather than discovered.

**D11 — the greeting's lifetime across navigation.** The banner is rendered
above the view sections, so navigating away 1 s after login carries it into the
next view for the remainder of its 4 s. Harmless (`aria-live`, non-blocking, no
backdrop); call it out rather than let a reviewer find it. Cheapest fix if
anyone objects: clear it in the `hashchange` handler's `navigated` branch.

---

## 6. Final recommended task order

Unchanged from the converged table (§1), with three adjustments:

1. **T3's evidence gate is now "hardware first, simulator second"** (C4). The PR
   must state which it used, and must not claim the hardware is dead.
2. **T1's copy sweep is 7 sites, not 5**: `security.py:167`, `auth.py:266`,
   `auth.py:271`, `schemas.py:154`, `index.html:139`, `README.md:135`,
   `NOTES.md:21` + `NOTES.md:279` — and explicitly **not** `items.py:466`.
3. **T0 additionally pins the Vue facts** the later tasks depend on, as
   comments in `tests/js/harness/dom.js`: text-ish → `input`, select → `change`,
   composing → ignored. Cheap, and it stops C1/C2 from being re-litigated during
   implementation.

Everything else stands: T0 ∥ T1 ∥ T2, then T3 → T4 → T5 → T6 serial, T7 last.

## 7. The single best design, per contested area

### 7.1 Capture interception (item 1) — canonical

`static/scan-capture.js` exports `createScanCapture(deps)` with injected
`now/setTimeout/clearTimeout/document/onScan/sinkSelector` (no Vue, no globals);
`app.js:929-1010` becomes a thin adapter — `onScan → enqueueScan`, keeping the
`scanCapture` flag that renders `data-scan-capture="on"` (`index.html:16`,
the `ui_check.py:293` pin). **The routing half is untouched**: pill masking,
queue, `drainScanQueue`, `handleBadgeScan`, `handleBarcodeScan`,
`scanFieldSubmit`, and both sinks.

States: `IDLE → HELD → BURST`, plus `WARM(el, 400 ms)` as a per-element
fast-path.

```
IDLE  ──printable, gap≥50ms, holdable target──▶ HELD   [preventDefault, arm 60ms]
HELD  ──printable, gap<50ms──────────────────▶ BURST  [preventDefault, accumulate,
                                                        RE-ARM the timer (L1)]
HELD  ──timer fires──────────────────────────▶ replay → WARM(el,400ms)
BURST ──timer fires──────────────────────────▶ replay → IDLE   (NOT WARM: sub-50ms
                                                        arrivals prove nothing)
HELD/BURST ──Enter, shape ok─────────────────▶ route; held chars dropped, never painted
HELD/BURST ──Enter, shape bad────────────────▶ replay → IDLE; Enter keeps its default
HELD/BURST ──printable, gap≥50ms─────────────▶ replay, then HELD(new char)
HELD/BURST ──non-printable/modifier/repeat/composing/paste/focus/pointerdown──▶ replay → pass through
WARM  ──printable────────────────────────────▶ native (no preventDefault); snapshot
                                                        value+caret on gap≥50ms keys
WARM  ──burst confirmed (2nd fast key)───────▶ retract to snapshot → BURST   [E1]
NO-WARM targets (D1c: password / autocomplete=username) ── always the HELD path
```

* `_buf` is maintained in **every** state so the Enter shape gate sees exactly
  today's string; only *where characters physically land* differs.
* **Target classification** — text-ish `INPUT`+`TEXTAREA`: hold + caret-splice
  replay; `type=number|date|time|month|week|color|range`: never held, `value`
  snapshot + retract (caret unreadable, C1/L2); `checkbox|radio|file|button|
  submit`: never held, preventDefault in burst; `SELECT`: never held,
  `selectedIndex` snapshot + restore + **`change`** dispatch (C2);
  `contentEditable`: never held (none exist); non-editable: native, preventDefault
  in burst (C8: nothing to break); `[data-scan-sink]` ancestors: **early return
  before all of it** (the collapsed badge fallback and the visible scan field
  keep their dedicated flows; the `ui_check.py:298` placeholder pin stays green).
* **Replay/retract**: `el.value = before + chars + after` from the snapshot,
  `setSelectionRange` in `try/catch`, dispatch plain `Event("input",{bubbles:true})`
  (C3). Stale-snapshot guard: flush on `paste`/`cut`/`drop`/non-`insertText`
  `beforeinput`, and if `el.value` differs from the snapshot at replay time,
  insert at the **current** caret.
* **Guards**: `compositionstart` (document) **and** `e.isComposing`/`keyCode 229`
  → flush + pass (C5); `e.repeat` → flush + clear WARM + pass; `ctrl/meta/alt` →
  reset; `e.key.length !== 1` → flush first, then pass; `pointerdown`/`focusin` →
  flush into the snapshotted element. **Never consult `isTrusted`** (contract
  test pins its absence).
* **`SCAN_DEFER_MS = 60 > SCAN_MAX_GAP_MS = 50` > scanner inter-key (10-20 ms)**
  — pinned in Node *and* pytest. `SCAN_STRICT_DEFER` (default false) closes **E1
  only**; **E2 is irreducible without a sentinel prefix** (L3) — say so in the
  PR rather than implying STRICT is a complete answer.
* Node matrix (~35 cases): scanner GTIN / `HCRM1:`+43 / bare-43 at 12/20/45 ms
  (zero painted chars, exact `onScan` payload, Enter prevented); human at
  55/80/100 ms (exact sequence, zero held chars at end, **one** preventDefault
  per run); the 50/60 ms boundary pairs; sink never prevented and its own handler
  fires; unshaped bursts inert (no `onScan`, no server call); timer-fire flush in
  HELD (→WARM) and in BURST (→IDLE, the dead-source case); repeat; composing
  (both the keydown flag and `compositionstart`); paste-while-held; focus change;
  pointerdown; caret preservation with pre-existing text; SELECT `selectedIndex` +
  `change`; number `value`-only; credential field never warms (D1c); unshaped
  Enter into a form still submits (D10); the inequality.

### 7.2 Calendar aggregate (item 5) — canonical

`GET /api/orders/calendar?year&month` — `Query(ge=2000, le=2100)` /
`Query(ge=1, le=12)`, both defaulting to the current **UTC** month;
`get_current_user` gate (so the `must_change_password` 403 and the
staff-sees-only-their-own rule come free). ORM range select on the indexed
`user_id` (`models.py:177`) over the half-open window
`[datetime(y, m, 1), datetime(y + (m == 12), m % 12 + 1, 1))`, bucketed in
Python by `created_at.date().isoformat()` — safe because of C6 (one writer, one
format, lexicographic == chronological). Response:
`{year, month, days: {"YYYY-MM-DD": {cents, count}}, total_cents,
purchase_count}`, where `cents` sums receipt `total_cents` (Σ price ×
**applied**_qty — the *paid* amount, so the clamp case is right by construction).
Schemas `MonthDayOut` / `OrderCalendarOut`, `extra="forbid"`, docstrings stating
the UTC-day rule; one-line comments at both ends (R6/Q5: UTC days are the only
bucketing where a dot never disagrees with its list row).
Tests (8): same-day aggregation; boundary instants in/out; empty month → 200 with
`days == {}`; user isolation; staff parity with `/mine`; clamp shows the paid
amount; 401/403/422; `days`-key format regex. Plus one `smoke_test.py` check.
Client: `monthMatrix` via `Date.UTC` (never local midnight), Monday-first,
padded; `dayColour` = `hsl(120·(1−t), 73%, 75−25t%)` with
`t = max > min ? (cents−min)/(max−min) : 0` over **day** totals;
`monthSummary` cross-checked against `total_cents` (drift detector); empty month
renders dotless with `€0.00`.

### 7.3 Review screen (item 4) — canonical

`#/checkout` → `currentView === "checkout"`, rendering the **same** `<section>`
as the catalogue (`v-else-if` gains `|| currentView === 'checkout'`,
`index.html:124`) with only the `<aside>` swapped to `.review-panel`: one grid,
one `#sentinel`, IntersectionObserver untouched, and "add" is the literal click
path plus global scan-to-add (a route is not a modal, so
`handleBarcodeScan`'s gate does not block). Topbar "Catalogue" active state gains
`|| currentView === 'checkout'`. **No new cart state** — the server cart *is* the
review state; every control is an existing `_cartOp`-serialized mutation
(`+`/`−`, `✕`, Clear, grid add, scan add) plus `setLineQty(id, raw)` →
`PATCH /cart/items/{id}` (absolute; `0` removes, `cart.py:290-292`; invalid/422 →
toast + `loadCart()` revert; D8 explains the visible snap-back).
`goToCheckout` replaces the `confirm()` path and assigns `this.route` +
`location.hash` in the same turn; the watcher loads items like the catalogue and
bounces an empty cart back with a toast; a logged-out deep link shows login.
`donePurchase` = today's `completePurchase` body minus `confirm()`:
`busy`-guarded, inside `_cartOp`, raw `POST /api/orders` with 400/401/503
branches; on 201 → `cart = data.cart`, `lastCheckout = id`,
`profileTab = "purchases"`, same-turn route to `#/account`, `loadMyOrders()`,
`expandedOrders = [id]`, **always** a toast (`Purchase complete — <total>`, or
the skipped-lines variant), and the receipt modal **only** when
`data.skipped.length || data.purchase.lines.some(l => l.clamped)` (D5/Q4).

---

## 8. Residual open questions for the coordinator

| # | Question | Default (all three plans) |
|---|---|---|
| Q1 | `SCAN_STRICT_DEFER` default | **false** (WARM on), plus **D1c** no-WARM for credential fields — which makes the strict flag nearly moot |
| Q2 | Pre-switch "Switch to \<name\>?" prompt (needs the incoming name in `QrIdentifyOut`) | **No** — enumeration/privacy cost; the post-switch toast already names both |
| Q4 | Receipt modal after "Done" | **Exception-only** (skipped/clamped) |
| Q5 | Calendar day bucketing | **UTC**, documented at both ends; `formatDate`'s local parse is a separate follow-up |
| Q6 | Logged-out `#/checkout` deep link | **Login, then catalogue** (no redirect-intent field exists) |
| Q7 | Month navigation on the calendar | **Ship it**; trim if the spec is read strictly |
| Q8 | Node in CI | **Separate `js` job**; missing toolchain fails, never skips |
| Q9 (new) | Adopt D1c / D7 / D11? | **Yes / yes / document-only** |
| Q10 (new) | Round-3.5 ticket for the M-10 **sentinel prefix** (the only thing that removes E2) | **Yes** — file it now, out of scope for round 3 |

---

## 9. Post-review audit — round 3 was implemented and merged while this review ran

**The tree moved.** HEAD is now `484c1c3`, and `git log 4e831e2..484c1c3` shows
the whole round landed as four PRs while the three plans were being written:

| commit | subject |
|---|---|
| `c17ad42` (PR #22) | `feat(orders): 60s logoff grace + purchase-history calendar aggregate` |
| `945fb71` (PR #23) | `feat(scan): intercept scans so text never lands in focused fields` |
| `1a2f907` (PR #24) | `feat(spa): greeting banner, checkout review screen, purchase-history calendar` |
| `501e85a` (PR #25) | `docs: round-3 scanner interception, greeting, checkout review, calendar` |

So §1-§8 above are now a *retrospective* on a merged design rather than a plan.
I audited the landed code against the converged design; the result is below, and
it is the useful output of this review.

### 9.1 Landed as designed (spot-verified, not assumed)

`static/scancapture.js` (316 lines) is a **pure reducer** —
`burstStep(state, ev) → {state, actions}` with injected time and an action
vocabulary (`prevent | native(snap) | replay | flush | retract | restore |
route`) — plus a thin adapter in `static/app.js:1121-1320`. It carries: the
`SCAN_DEFER_MS (60) > SCAN_MAX_GAP_MS (50)` inequality with the load-bearing
comment (`scancapture.js:57-62`); WARM/`SCAN_WARM_MS = 400`;
`SCAN_STRICT_DEFER = false`; the **timer re-arm on every key** (my L1, adopted —
`app.js:1201-1211`, with the same "cannot fire mid-scan" proof); the
text/value/select/plain target classification (`app.js:1150-1172`); the
stale-snapshot fallback (caret **re-read at replay time**, `app.js:1296-1303`);
`pointerdown`/`focusin`/`paste` external flushes (`app.js:1140-1142`);
repeat/composing/modifier guards routed through one `_guard()`; the sink
early-return as the first statement (`app.js:1176-1180`); dual-mode export
(`module.exports` / `window.HcrmScanCapture`, `scancapture.js:311-315`); the
routing bus untouched (`enqueueScan` still masks badges); and the SELECT fix
restoring `selectedIndex` **and dispatching `change`** (`app.js:1290`).
`tests/js/scancapture.test.mjs` (407 lines) drives it with an injected clock;
`tests/test_frontend_contract.py` pins the inequality, the script order, the two
sinks, and the absence of `isTrusted`. Grace is `60` (`security.py:167`),
`GET /api/orders/calendar` exists (`orders.py:220`), the greeting is a banner set
in `saveSession` and deliberately **not** cleared in `_closeOverlays`
(`app.js:520-531` — the comment is verbatim the reasoning in §2.1/§2.2),
`#/checkout` reuses the catalogue `<section>` (`index.html:138`) with a swapped
aside (`index.html:220`), the receipt modal is exception-only (`app.js:1104`),
and `dayColor` is the exact HSL ramp from §7.2 (`app.js:999-1008`).

### 9.2 Where the landed code is **better** than all three plans

1. **`setRangeText(buffer, start, end, "end")`** (`app.js:1305`) instead of the
   manual `value.slice()` splice all three of us wrote — it is the standard API,
   and it participates in the native undo stack where supported, which softens
   kimi's risk 7 / my R4(b).
2. **A coercion guard on the value-only write path** (`app.js:1310-1317`): if the
   browser rejects the appended value for the type (number/date empty it), the
   previous value is restored instead of silently wiping the field. None of us
   thought of that failure mode.
3. **External signals end the WARM window** (`scancapture.js:180-188`) with the
   reason spelled out: the reducer cannot see element identity, so a warm window
   surviving a focus change would let a later retract restore a *different*
   field's snapshot and eat the text typed there. That is a real hazard none of
   the three plans named — my design tracked WARM per element in the adapter and
   would have needed the same rule stated explicitly.
4. **The calendar endpoint computes paid amounts by joining `PurchaseLine`**
   (Σ `price_cents × applied_qty`, `orders.py:255-266`) rather than trusting
   `Purchase.total_cents`, and caps the half-open range at `datetime.max` for
   year 9999-12 so a *validated* input can never 500 (`orders.py:248-253`).
   My T2 specified `ge=2000, le=2100` and simply summed `total_cents` — the
   landed version is both more faithful to clamp-not-veto and more defensive.
5. **The CI answer to Q8 beats all three proposals**: no new job and no pytest
   dependency on Node — a separate *step* in the existing pytest job,
   `node --test tests/js/*.test.mjs` (`ci.yml:53-59`), with a comment explaining
   why the glob form matters (`node --test <dir>` tries to load the directory as
   a module). `uv run pytest` stays Node-free locally; a missing node fails the
   step, per the repo's never-skip doctrine.

### 9.3 One finding I raised and then **refuted by measurement**

I suspected the landed classification put `type="email"` in the value-only class
by mistake (email is a text-entry type, I assumed, so its caret is readable and
an append-replay would corrupt mid-address edits — `index.html:111`, `:262`,
`:611`). Measured in headless Chrome on this host
(`google-chrome --headless=new --dump-dom`, `setSelectionRange` per type):

```
text  sel=2   email THROWS InvalidStateError   number THROWS   tel   sel=2
search sel=2  url   sel=2                      password sel=2  date  THROWS
datetime-local THROWS
```

So `email` **does not** support the selection API, and `app.js:1163-1169`
(text-ish = `text, search, tel, url, password, ""`; value-only = `email, number,
date, time, month, week, datetime-local`) is **exactly right**. Recorded here so
nobody "fixes" it later on the same faulty recollection I had — and as evidence
that the classification table in §3.3 of my plan should be read as
"selection-supporting vs not", not "text-ish vs exotic".

### 9.4 What is still open (all low severity, none blocking)

1. **No `compositionstart` flush.** The external listeners are
   `pointerdown`/`focusin`/`paste` (`app.js:1140-1142`); composition is handled
   only by the keydown flag (`ev.composing`, `app.js:1189`). Vue's `vModelText`
   ignores `input` while `el.composing` (verified in the vendored bundle), so a
   replay landing *during* a composition would update the DOM and not the model.
   Platforms where the first composing keydown does not yet carry `isComposing`
   are the exposure. One line:
   `document.addEventListener("compositionstart", this._onScanExternal, true)`.
2. **D1c was not implemented**: credential fields warm like any other text
   field, so E1 (one character, ≤1 frame) still applies to the **autofocused
   `type="text"` login username field** (`index.html:55-56`) — the one place
   where a painted character is a fragment of a bearer credential, and the exact
   exposure §0 documents. Cheap to close: one selector test in `_scanClass`
   (or a `noWarm` flag on the event) so `password`/`autocomplete="username"`
   always take the held path.
3. **The greeting is `v-if` + `role="status"`** (`index.html:28`) rather than a
   persistent live region with conditional text — announcement of an *inserted*
   live region is inconsistent across screen readers (my L5). It did gain
   `@click="dismissGreeting"`, which none of the three plans had and is a good
   addition.
4. **E2 remains** (a scan starting <50 ms after a human keystroke in the same
   field is not intercepted). Unchanged from before round 3, mode-independent,
   and only removable by the sentinel-prefix follow-up (Q10) — worth filing now
   that the heuristic version has shipped.

### 9.5 Status of my own plan file

`docs/plans/r3plan-qwen.md` is rev 2 of a plan whose subject is now merged. Its
`file:line` citations were verified against **`4e831e2`** and are stale for
`static/app.js` (+517 lines), `static/index.html` (+177) and
`app/routers/orders.py` (+72); a note to that effect is at the top of that file.
Its value now is as the design rationale + the exception taxonomy (E1/E2) + the
verification measurements (C1-C8, §9.3), which the merged code's comments only
partly carry.
