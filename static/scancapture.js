/* HCRM scan capture — the pure burst-interception state machine (round 3, Task B).
 *
 * The M-10 in USB-HID mode is a keyboard wedge: it types a burst of
 * characters far faster than a human (inter-key 10-20 ms vs 80 ms+) and
 * ends with Enter. This module decides, from a key/timing stream alone,
 * whether each key belongs to a scan burst (SWALLOW it — it must never
 * paint into a focused field) or to a human (let it land, and write back
 * anything that was provisionally swallowed). All DOM work —
 * preventDefault, timers, snapshots, the splice/replay write paths —
 * lives in the app.js adapter; this file is PURE so `node --test`
 * (tests/js/scancapture.test.mjs) can drive it with scripted streams.
 *
 * States (mode):
 *   IDLE  — nothing pending.
 *   HOLD  — a run has started with ONE unconfirmed character: for
 *           holdable targets (text/value) it is prevented and held for
 *           SCAN_DEFER_MS; pass-through targets (SELECT & friends) let it
 *           land natively (typeahead must not break) but snapshot first.
 *   ARMED — a fast second key confirmed machine timing: every further
 *           printable is prevented until the Enter shape-gates the buffer
 *           (route) or the run aborts (replay/restore).
 *   WARM  — a run was proven human and flushed: for SCAN_WARM_MS the
 *           target passes keys NATIVELY (zero typing latency), with a
 *           pre-key snapshot taken so a burst that starts inside the
 *           window is retracted (the one bounded artifact: one character
 *           for one frame, only when a scan begins within 400 ms of human
 *           typing in the same field). SCAN_STRICT_DEFER=true disables
 *           WARM for the absolute "never a pixel" guarantee, at the cost
 *           of holding every character for the defer window.
 *
 * Events: {type:"key", key, t, cls, repeat, composing, mod} and
 * {type:"timer"|"external", t} (the defer timer / pointerdown / focusin /
 * paste — all "a human is verifiably present" signals). `t` is a
 * performance.now()-style injected clock; `cls` is the adapter's target
 * classification: "text" (selection-API inputs + textarea), "value"
 * (number/email/date — no selection API, value-only writes), "select",
 * "plain" (non-editable, contentEditable included — pass-through, never
 * hijacked). Sinks ([data-scan-sink]) never reach the reducer: the
 * adapter returns before calling it.
 *
 * Actions the adapter applies, in order:
 *   prevent          — preventDefault this key (nothing may land).
 *   native {snap}    — let the key land; take a pre-key snapshot if snap.
 *   replay {buffer, cls} — write the held chars back into the target
 *                          (text: setRangeText at the CURRENT caret; value:
 *                          value-append + coercion check).
 *   flush {buffer, cls} — same write-back, emitted when the human verdict
 *                          came from the timer/external signals.
 *   restore          — put a SELECT back to its run-start selectedIndex
 *                      (undoes armed-burst typeahead churn).
 *   retract          — restore the warm-window snapshot (undo the char(s)
 *                      that landed natively just before a burst confirmed).
 *   route {code, kind, restore} — a confirmed scan: preventDefault the
 *                          Enter, restore the select if needed, and hand
 *                          (code, kind) to the unchanged routing bus.
 *
 * The load-bearing inequality: SCAN_DEFER_MS (60) > SCAN_MAX_GAP_MS (50)
 * > scanner inter-key (10-20 ms). The defer timer is (re)armed on EVERY
 * held key, so a burst whose source dies mid-payload cannot hang the
 * held state — and it can never fire mid-scan, because the gap between
 * two scan keys is always well under the window. Pinned in Node AND
 * pytest (tests/test_frontend_contract.py).
 *
 * Dual-mode export: browser global `HcrmScanCapture` + CommonJS
 * `module.exports` (no package.json in the tree, so Node treats .js as
 * CJS; the SPA loads it as a classic script before app.js).
 */

const SCAN_MAX_GAP_MS = 50;   // machine/human boundary (scanner 10-20 ms, humans 80 ms+)
const SCAN_DEFER_MS = 60;     // MUST exceed SCAN_MAX_GAP_MS (see above)
const SCAN_WARM_MS = 400;     // proved-human native window after a flush
const SCAN_MIN_CHARS = 4;     // the shape gate's length floor
const SCAN_STRICT_DEFER = false;  // true = no WARM fast-path: every char held

// Routing-only shape gates (the SERVER classifies authoritatively — these
// just pick the flow and keep garbage bursts from opening modals).
// Python source of truth: app/scanshapes.py (round 4, T1) — the badge
// pattern TEXT is pinned equal on both sides by tests/test_scanshapes.py.
const BADGE_FULL_RE = /^HCRM1:[A-Za-z0-9_-]{43}$/;
const BADGE_RAW_RE = /^[A-Za-z0-9_-]{43}$/;          // prefix stripped by a bridge/scanner
// Barcode-ish: GTIN digits, or an opaque label — which always carries at
// least one digit (SKUs, internal codes). A digit-free word is typing, not
// a scan: ignored before any server call.
const BARCODE_DIGITS_RE = /^[0-9]{4,14}$/;
const BARCODE_OPAQUE_RE = /^(?=.*\d)[A-Za-z0-9._/-]{4,64}$/;

function scanShape(code) {
  if (BADGE_FULL_RE.test(code) || BADGE_RAW_RE.test(code)) return "badge";
  if (BARCODE_DIGITS_RE.test(code) || BARCODE_OPAQUE_RE.test(code)) return "barcode";
  return null; // not scan-shaped: ignore, no server call
}

function isBadgeShape(code) {
  return BADGE_FULL_RE.test(code) || BADGE_RAW_RE.test(code);
}

const IDLE = "idle";
const HOLD = "hold";
const ARMED = "armed";
const WARM = "warm";

function initialState(t = 0) {
  return { mode: IDLE, buf: "", lastT: t, warmUntil: 0, warmNative: false, cls: null };
}

function _idle(t) {
  return { mode: IDLE, buf: "", lastT: t, warmUntil: 0, warmNative: false, cls: null };
}

function _warm(t, lastT) {
  // lastT is preserved from the run's last KEY (not the flush's clock):
  // the next key's fast/slow gap must be measured against real keystrokes —
  // a human key 40 ms after the timer fired is still 100+ ms after the last
  // key it flushed.
  return { mode: WARM, buf: "", lastT: lastT === undefined ? t : lastT,
           warmUntil: t + SCAN_WARM_MS, warmNative: false, cls: null };
}

/** The write-back owed when a run ends: held chars must reach their target.
 * text/value runs were prevented from char 1 → replay the whole buffer.
 * A select's ARMED churn is undone by the restore (an unconfirmed single
 * typeahead char is legitimate and stays). rich/plain have no write path
 * (plain targets have nothing to insert; their chars landed natively).
 * `flavor` names the verdict: "flush" = the SCAN_DEFER_MS timer / a
 * pointer-focus-paste signal proved a human; "replay" = a key-driven
 * abort (slow gap, guard key, unshaped Enter). Same write either way. */
function _endRunActions(state, flavor) {
  if (!state.buf) return [];
  if (state.cls === "text" || state.cls === "value") {
    return [{ type: flavor, buffer: state.buf, cls: state.cls }];
  }
  if (state.cls === "select" && state.mode === ARMED) {
    return [{ type: "restore" }];
  }
  return [];
}

function _afterRunEnd(state, t, warm) {
  return warm && !SCAN_STRICT_DEFER ? _warm(t, state.lastT) : _idle(t);
}

/** In WARM: keep the window alive (the key proves continued presence);
 *  expired (or never warm) → IDLE. */
function _warmRefresh(state, t) {
  if (state.mode !== WARM || t >= state.warmUntil || SCAN_STRICT_DEFER) {
    return _idle(t);
  }
  // buf and warmNative are CLEARED, not carried: the retract insurance is
  // only valid for the char immediately after its snapshot. The event that
  // led here (a guard key, an Enter) landed natively WITHOUT a snapshot, so
  // keeping the insurance would let a later burst-confirm retract restore
  // the stale snapshot and eat that unsnapshotted text.
  return { ...state, lastT: t, warmUntil: t + SCAN_WARM_MS,
           buf: "", warmNative: false };
}

/** Guard keys (repeat / composing / modifiers / non-printables): end any
 *  pending run, then let the key through untouched. `warm` says whether
 *  the guard itself proves human rhythm (repeat, non-printables do;
 *  modifiers and IME composition reset to idle — a shortcut or a
 *  composition session is a new context). */
function _guard(state, t, warm) {
  if (state.mode === HOLD || state.mode === ARMED) {
    return {
      state: _afterRunEnd(state, t, warm),
      actions: [..._endRunActions(state, "replay"), { type: "native" }],
    };
  }
  return { state: _warmRefresh(state, t), actions: [{ type: "native" }] };
}

/** burstStep(state, event) → {state, actions} — the pure reducer. */
function burstStep(state, ev) {
  const t = ev.t;

  // --- the human verdict: the defer timer, or pointer/focus/paste -------
  if (ev.type === "timer" || ev.type === "external") {
    if (state.mode === HOLD || state.mode === ARMED) {
      return { state: _afterRunEnd(state, t, true),
               actions: _endRunActions(state, "flush") };
    }
    // An external signal ENDS the warm window: the window is per-context
    // and the reducer cannot see element identity — a window that survives
    // a focus change lets a later burst-confirm retract restore a
    // DIFFERENT field's snapshot, eating the human text typed there.
    // (Cost: the next key re-earns warm after one defer-window hold.)
    if (ev.type === "external" && state.mode === WARM) {
      return { state: _idle(t), actions: [] };
    }
    return { state, actions: [] };   // idle (or warm + timer): nothing pending
  }

  const cls = ev.cls;

  // --- guard keys: flush first, then pass through -----------------------
  if (ev.composing) return _guard(state, t, false);
  if (ev.mod) return _guard(state, t, false);
  if (ev.repeat) return _guard(state, t, true);

  if (ev.key === "Enter") {
    if (state.mode === HOLD || state.mode === ARMED) {
      const kind = state.buf.length >= SCAN_MIN_CHARS ? scanShape(state.buf) : null;
      if (kind) {
        // A confirmed scan: nothing ever landed (text/value), the select's
        // churn is undone, and the Enter itself must not submit anything.
        // The select's restore rides ON the route action (restore: true) —
        // a standalone restore here would run TWICE (the adapter restores
        // on route.restore), dispatching a duplicate `change`.
        const actions = [{ type: "prevent" }];
        actions.push({ type: "route", code: state.buf, kind, restore: state.cls === "select" });
        return { state: _idle(t), actions };
      }
      // Unshaped: write the held chars back FIRST, then the Enter keeps
      // its default (a form submit must see the full value).
      return {
        state: _afterRunEnd(state, t, false),
        actions: [..._endRunActions(state, "replay"), { type: "native" }],
      };
    }
    // warm/idle: anything buffered already landed natively — nothing to write.
    return { state: _warmRefresh(state, t), actions: [{ type: "native" }] };
  }

  if (typeof ev.key !== "string" || ev.key.length !== 1) {
    // Non-printable (Backspace, Tab, arrows, a plain Shift…): the pending
    // chars must be in the field BEFORE the key acts on them.
    return _guard(state, t, true);
  }

  // --- printable --------------------------------------------------------
  const fast = t - state.lastT < SCAN_MAX_GAP_MS;
  const holdable = cls === "text" || cls === "value";
  const runActive = state.mode === HOLD || state.mode === ARMED;
  // A focus change mid-run (focusin normally flushes first — defensive).
  const classChanged = runActive && state.cls !== cls;

  let actions = [];
  let base = state;
  if (runActive && (!fast || classChanged)) {
    // A slow gap (or a target change) ends the run as human text and opens
    // the warm window — the CURRENT key then lands natively (zero latency).
    actions = _endRunActions(state, "replay");
    base = _afterRunEnd(state, t, true);
  }

  const inWarm = base.mode === WARM && t < base.warmUntil && !SCAN_STRICT_DEFER;

  if (inWarm && fast && base.warmNative) {
    // A burst starting inside the warm window. Retract what landed
    // natively since the last snapshot (holdable targets; the select's
    // churn is undone by the end-of-run restore instead), then hold.
    const confirm = [];
    if (holdable) confirm.push({ type: "retract" });
    if (holdable || cls === "select") confirm.push({ type: "prevent" });
    else confirm.push({ type: "native" });   // plain: nothing to paint
    return {
      state: { mode: ARMED, buf: base.buf + ev.key, lastT: t, warmUntil: 0,
               warmNative: false, cls },
      actions: [...actions, ...confirm],
    };
  }

  if (inWarm && !fast && holdable) {
    // Continued human typing inside the window: native, snapshotted (the
    // retract insurance), and the logical buffer resets to this char — a
    // slow-typed accumulation can never reach the Enter shape gate.
    return {
      state: { mode: WARM, buf: ev.key, lastT: t, warmUntil: t + SCAN_WARM_MS,
               warmNative: true, cls: null },
      actions: [...actions, { type: "native", snap: true }],
    };
  }

  if (runActive && fast && !classChanged) {
    // Burst continuation: keep preventing (plain targets have no insertion
    // point — preventing there buys nothing and can eat a Space scroll).
    const prevent = holdable || cls === "select";
    return {
      state: { mode: ARMED, buf: state.buf + ev.key, lastT: t, warmUntil: 0,
               warmNative: false, cls: state.cls },
      actions: [...actions, prevent ? { type: "prevent" } : { type: "native" }],
    };
  }

  // Run start (idle, warm-expired, or right after a run ended): the first
  // character is ALWAYS buffered — an earlier revision skipped it and
  // corrupted every capture (a GTIN lost its first digit, a badge the 'H'
  // of HCRM1:). Holdable targets hold it; a select lets it land (single-
  // char typeahead is human) but snapshots first.
  const start = holdable
    ? [{ type: "prevent" }]
    : cls === "select"
      ? [{ type: "native", snap: true }]
      : [{ type: "native" }];
  return {
    state: { mode: HOLD, buf: ev.key, lastT: t, warmUntil: 0, warmNative: false, cls },
    actions: [...actions, ...start],
  };
}

const HcrmScanCapture = {
  SCAN_MAX_GAP_MS,
  SCAN_DEFER_MS,
  SCAN_WARM_MS,
  SCAN_MIN_CHARS,
  SCAN_STRICT_DEFER,
  scanShape,
  isBadgeShape,
  initialState,
  burstStep,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = HcrmScanCapture;
}
if (typeof window !== "undefined") {
  window.HcrmScanCapture = HcrmScanCapture;
}
