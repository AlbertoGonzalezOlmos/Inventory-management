// Scan-capture reducer tests (round 3, Task B) — `node --test tests/js/`.
//
// The reducer (static/scancapture.js) is pure: no DOM, no timers, the clock
// is injected. These tests drive it with scripted key streams — scanner
// bursts (10-20 ms gaps), slow human typing (80 ms+), fast-unshaped typing,
// repeats, guards, timers — and assert the ACTION sequences, which are the
// adapter's exact instructions (prevent / native / replay / flush / restore /
// retract / route). The DOM side of every assertion (preventDefault, the
// splice, the snapshot) is exercised by the adapter contract + ui_check.
//
// Timing fixtures: 12 ms = a typical scanner inter-key; 20 ms = the scanner's
// slow side; 45 ms = the worst-case scanner still under the 50 ms gate;
// 55 ms = the fastest plausible human (just over it); 80-120 ms = human.

import test from "node:test";
import assert from "node:assert/strict";

import HcrmScanCapture from "../../static/scancapture.js";

const {
  SCAN_MAX_GAP_MS, SCAN_DEFER_MS, SCAN_MIN_CHARS, SCAN_WARM_MS,
  scanShape, isBadgeShape, initialState, burstStep,
} = HcrmScanCapture;

const T0 = 1_000_000;   // an arbitrary performance.now()-style epoch

function key(ch, t, cls = "text", extra = {}) {
  return { type: "key", key: ch, t, cls, repeat: false, composing: false,
           mod: false, ...extra };
}
const timer = (t) => ({ type: "timer", t });
const external = (t) => ({ type: "external", t });

/** Drive a stream through the reducer, collecting every action. */
function drive(events, cls = "text") {
  let state = initialState(T0);
  const actions = [];
  for (const ev of events) {
    const r = burstStep(state, ev.type === "key" && ev.cls === undefined
      ? { ...ev, cls } : ev);
    state = r.state;
    actions.push(...r.actions);
  }
  return { state, actions };
}

const types = (acts) => acts.map((a) => a.type);
const count = (acts, type) => acts.filter((a) => a.type === type).length;
const routes = (acts) => acts.filter((a) => a.type === "route");

const GTIN = "4006381333931";                       // EAN-13
const BADGE_RAW = "aB3xY9kQ2mZ4nL8pR7sT1uV6wW0eF5gHjIiKq_oPz-XyZ0".slice(0, 43);
const BADGE_FULL = "HCRM1:" + BADGE_RAW;

/** A scanner burst: `code`'s chars at `gap` ms, then Enter. */
function burst(code, gap = 12, cls = "text") {
  const events = [];
  let t = T0 + 100;
  for (const ch of code) { events.push(key(ch, t, cls)); t += gap; }
  events.push(key("Enter", t, cls));
  return events;
}

// --- constants: the load-bearing inequality (pinned here AND in pytest) ------

test("SCAN_DEFER_MS exceeds SCAN_MAX_GAP_MS", () => {
  // If the defer window ever drops below the burst gate, a scan's second
  // key arrives after the human-verdict flush and the payload paints.
  assert.ok(SCAN_DEFER_MS > SCAN_MAX_GAP_MS,
    `SCAN_DEFER_MS (${SCAN_DEFER_MS}) must exceed SCAN_MAX_GAP_MS (${SCAN_MAX_GAP_MS})`);
  assert.equal(SCAN_MIN_CHARS, 4);
  assert.ok(SCAN_WARM_MS >= 100);   // the warm window must outlive a typing gap
});

// --- shape gates --------------------------------------------------------------

test("scanShape known answers", () => {
  assert.equal(scanShape(BADGE_FULL), "badge");
  assert.equal(scanShape(BADGE_RAW), "badge");
  assert.equal(scanShape(GTIN), "barcode");
  assert.equal(scanShape("SKU-42/a"), "barcode");       // opaque with a digit
  assert.equal(scanShape("hello"), null);               // digit-free word: typing
  assert.equal(scanShape("12"), null);                  // under the length floor
  assert.equal(isBadgeShape(BADGE_FULL), true);
  assert.equal(isBadgeShape(GTIN), false);
});

// --- invariant 1: a shape-gated burst never writes ---------------------------

test("GTIN burst into a text field: prevented throughout, routed, never written",
     () => {
  const { state, actions } = drive(burst(GTIN));
  // every printable was prevented…
  assert.equal(count(actions, "prevent"), GTIN.length + 1);   // chars + the Enter
  // …nothing was ever written back…
  assert.equal(count(actions, "replay"), 0);
  assert.equal(count(actions, "flush"), 0);
  assert.equal(count(actions, "native"), 0);
  // …and exactly one scan was routed, with the exact code.
  assert.deepEqual(routes(actions),
    [{ type: "route", code: GTIN, kind: "barcode", restore: false }]);
  assert.equal(state.mode, "idle");
  assert.equal(state.buf, "");
});

test("badge bursts (full and raw-43) route as badge", () => {
  for (const code of [BADGE_FULL, BADGE_RAW]) {
    const { actions } = drive(burst(code));
    assert.deepEqual(routes(actions),
      [{ type: "route", code, kind: "badge", restore: false }]);
    assert.equal(count(actions, "replay") + count(actions, "flush"), 0);
  }
});

test("a worst-case-slow scanner (45 ms gaps) is still intercepted", () => {
  const { actions } = drive(burst(GTIN, 45));
  assert.equal(routes(actions).length, 1);
  assert.equal(count(actions, "prevent"), GTIN.length + 1);
});

// --- invariant 2: human typing is native after the first char of a run -------

test("slow human typing: one hold + one flush, then all native", () => {
  const events = [];
  let t = T0 + 100;
  for (const ch of "hello world") {
    events.push(key(ch, t));
    events.push(timer(t + SCAN_DEFER_MS));   // the human verdict arrives first
    t += 120;
  }
  const { state, actions } = drive(events);
  // The FIRST char is held (prevented) and flushed 60 ms later; every
  // subsequent char lands natively inside the warm window.
  assert.equal(count(actions, "prevent"), 1);
  assert.equal(count(actions, "flush"), 1);
  assert.deepEqual(actions.find((a) => a.type === "flush"),
    { type: "flush", buffer: "h", cls: "text" });
  assert.equal(count(actions, "native"), "hello world".length - 1);
  assert.equal(routes(actions).length, 0);
  // The logical buffer never accumulates slow-typed text (the shape gate
  // can only ever see machine-speed runs)…
  assert.equal(state.buf.length, 1);
  assert.equal(state.mode, "warm");
});

test("a pause longer than the warm window re-arms the hold exactly once", () => {
  const events = [
    key("a", T0 + 100), timer(T0 + 100 + SCAN_DEFER_MS),        // held + flushed
    key("b", T0 + 300),                                         // warm → native
    key("c", T0 + 3000),   // > SCAN_WARM_MS after b: the window expired
  ];
  const { actions } = drive(events);
  assert.equal(count(actions, "prevent"), 2);   // "a" (cold) and "c" (post-pause)
  assert.equal(count(actions, "flush"), 1);    // only "a" — "b" landed natively
  assert.equal(count(actions, "native"), 1);   // only "b"
});

// --- invariant 5: unshaped bursts are inert -----------------------------------

test("fast-unshaped burst + Enter: replayed, never routed", () => {
  const { state, actions } = drive(burst("!!xy"));   // no digit → unshaped
  assert.equal(routes(actions).length, 0);
  assert.equal(count(actions, "prevent"), 4);        // the chars were held
  // The Enter is NOT prevented (its default — submit/blur — must run), and
  // the held chars are written back BEFORE it.
  const kinds = types(actions);
  assert.equal(kinds[kinds.length - 1], "native");
  assert.deepEqual(actions.find((a) => a.type === "replay"),
    { type: "replay", buffer: "!!xy", cls: "text" });
  assert.ok(kinds.indexOf("replay") < kinds.length - 1);   // replay before Enter
  assert.equal(state.mode, "idle");
});

test("a short burst under the length floor is inert", () => {
  const { actions } = drive(burst("123", 12));   // 3 chars < SCAN_MIN_CHARS
  assert.equal(routes(actions).length, 0);
  assert.deepEqual(actions.find((a) => a.type === "replay"),
    { type: "replay", buffer: "123", cls: "text" });
});

test("a digit-free word typed fast is never routed", () => {
  const { actions } = drive(burst("qwertz", 12));
  assert.equal(routes(actions).length, 0);
  assert.equal(count(actions, "replay"), 1);
});

// --- invariant 6: no keystroke is ever lost ----------------------------------

test("the defer timer mid-burst (scanner died) flushes the held chars", () => {
  const events = [
    key("4", T0 + 100), key("0", T0 + 112), key("0", T0 + 124),
    timer(T0 + 124 + SCAN_DEFER_MS),   // no Enter ever comes
  ];
  const { state, actions } = drive(events);
  assert.equal(routes(actions).length, 0);
  assert.deepEqual(actions.find((a) => a.type === "flush"),
    { type: "flush", buffer: "400", cls: "text" });
  assert.equal(state.mode, "warm");   // proved human → the native window
});

test("pointerdown / focusin / paste flush like the timer", () => {
  const { state, actions } = drive([
    key("4", T0 + 100), key("0", T0 + 112), external(T0 + 200),
  ]);
  assert.deepEqual(actions.find((a) => a.type === "flush"),
    { type: "flush", buffer: "40", cls: "text" });
  assert.equal(state.mode, "warm");
});

test("non-printable keys flush first, then act natively", () => {
  const { actions } = drive([
    key("4", T0 + 100), key("0", T0 + 112),
    key("Backspace", T0 + 250),    // a human pause + a non-printable
  ]);
  const kinds = types(actions);
  assert.equal(kinds[kinds.length - 1], "native");
  assert.deepEqual(actions.find((a) => a.type === "replay"),
    { type: "replay", buffer: "40", cls: "text" });
  // the write-back precedes the Backspace so it deletes real text
  assert.ok(kinds.indexOf("replay") < kinds.length - 1);
});

test("held-key auto-repeat (e.repeat) is human: flush + native", () => {
  const { state, actions } = drive([
    key("4", T0 + 100), key("0", T0 + 112),
    key("0", T0 + 130, "text", { repeat: true }),
  ]);
  assert.equal(count(actions, "prevent"), 2);
  assert.deepEqual(actions.find((a) => a.type === "replay"),
    { type: "replay", buffer: "40", cls: "text" });
  assert.equal(types(actions).pop(), "native");
  assert.equal(state.mode, "warm");
});

test("IME composition passes through and flushes", () => {
  const { state, actions } = drive([
    key("4", T0 + 100), key("0", T0 + 112),
    key("Process", T0 + 200, "text", { composing: true }),
  ]);
  assert.equal(count(actions, "prevent"), 2);      // only the run's chars
  assert.equal(types(actions).pop(), "native");
  assert.equal(state.mode, "idle");                 // composition = new context
});

test("modifier shortcuts pass through and reset", () => {
  const { state, actions } = drive([
    key("4", T0 + 100), key("0", T0 + 112),
    key("c", T0 + 200, "text", { mod: true }),     // Ctrl+C
  ]);
  assert.equal(types(actions).pop(), "native");
  assert.equal(state.mode, "idle");
});

// --- warm-window retraction ---------------------------------------------------

test("a burst starting inside a warm window retracts the landed char", () => {
  const events = [
    key("h", T0 + 100), timer(T0 + 100 + SCAN_DEFER_MS),   // hold + flush → warm
    key("i", T0 + 200),                                    // native (warm), snapped
    key("4", T0 + 212), key("0", T0 + 224),                // a scan begins!
  ];
  const { state, actions } = drive(events);
  // the warm char "i" is retracted, the scan chars are prevented from here on
  assert.equal(count(actions, "retract"), 1);
  const i = types(actions).indexOf("retract");
  assert.equal(types(actions)[i + 1], "prevent");
  assert.equal(state.mode, "armed");
  assert.equal(state.buf, "i40");   // the logical buffer keeps the full string
});

test("an external signal in warm ENDS the window (no cross-field retract)", () => {
  // Type in field A (warm, snapshotted), then a pointerdown/focusin — the
  // window must die, or a burst confirming in field B would retract A's
  // snapshot and eat the human text typed there.
  const { state, actions } = drive([
    key("h", T0 + 100), timer(T0 + 100 + SCAN_DEFER_MS),   // hold + flush → warm
    key("i", T0 + 200),                                    // native (warm), snapped
    external(T0 + 250),                                    // the click away
    key("4", T0 + 260), key("0", T0 + 272),                // a scan begins in B
  ]);
  assert.equal(count(actions, "retract"), 0);   // A's snapshot never touched
  // the scan's first char starts a fresh HOLD (held, not landed+retracted)
  assert.equal(count(actions, "prevent"), 3);   // h, "4", "0"
  assert.equal(state.mode, "armed");
  assert.equal(state.buf, "40");   // and ONLY the scan's own chars
});

test("a guard key in warm drops the retract insurance", () => {
  // A warm-native char is snapshotted; an auto-repeat key then lands
  // natively WITHOUT a snapshot. If the insurance survived, a burst
  // confirming after it would retract to the stale snapshot and eat the
  // repeated text — so the guard must clear buf/warmNative, and the next
  // fast key starts a fresh HOLD instead of retracting.
  const { state, actions } = drive([
    key("h", T0 + 100), timer(T0 + 100 + SCAN_DEFER_MS),   // hold + flush → warm
    key("a", T0 + 200),                                    // native (warm), snapped
    key("a", T0 + 230, "text", { repeat: true }),           // auto-repeat: native
    key("4", T0 + 260), key("0", T0 + 272),                // a scan begins
  ]);
  assert.equal(count(actions, "retract"), 0);   // the repeated text is safe
  assert.equal(count(actions, "prevent"), 3);   // h, "4", "0"
  assert.equal(state.mode, "armed");
  assert.equal(state.buf, "40");
});

test("warm typing continues natively with per-key snapshots", () => {
  const events = [
    key("h", T0 + 100), timer(T0 + 100 + SCAN_DEFER_MS),
    key("e", T0 + 200), key("l", T0 + 290), key("l", T0 + 380),
  ];
  const { actions } = drive(events);
  assert.equal(count(actions, "retract"), 0);
  assert.equal(count(actions, "prevent"), 1);   // only the initial "h"
  const natives = actions.filter((a) => a.type === "native");
  assert.equal(natives.length, 3);
  assert.ok(natives.every((a) => a.snap === true));   // retract insurance
});

// --- per-target classes --------------------------------------------------------

test("SELECT: single typeahead char is native; an armed burst is restored", () => {
  // A human pressing one letter into a select (typeahead) must not be
  // prevented, and the flush may not undo their navigation (the restore is
  // reserved for ARMED bursts).
  const single = drive([key("g", T0 + 100, "select"),
                        timer(T0 + 100 + SCAN_DEFER_MS)]);
  assert.deepEqual(types(single.actions), ["native"]);   // run start: native + snap
  assert.equal(single.actions[0].snap, true);
  assert.equal(count(single.actions, "prevent"), 0);
  assert.equal(count(single.actions, "restore"), 0);
  assert.equal(count(single.actions, "flush"), 0);   // select has no write path
  assert.equal(single.state.mode, "warm");

  // A scan into a select: char 1 lands (typeahead churn), chars 2+ are
  // prevented, the Enter routes with the restore riding ON the route
  // action — a standalone restore would run twice in the adapter (a
  // duplicate `change` dispatch → a duplicate v-model reload).
  const scan = drive(burst(GTIN, 12, "select"));
  assert.equal(routes(scan.actions).length, 1);
  assert.deepEqual(routes(scan.actions),
    [{ type: "route", code: GTIN, kind: "barcode", restore: true }]);
  assert.equal(count(scan.actions, "restore"), 0);   // no duplicate restore
  // char 1 native+snapped, chars 2..13 prevented, Enter prevented
  assert.equal(count(scan.actions, "prevent"), GTIN.length);
  assert.equal(count(scan.actions, "native"), 1);
});

test("plain targets (body, buttons, contentEditable) are never prevented", () => {
  const { actions } = drive(burst(GTIN, 12, "plain"));
  assert.equal(count(actions, "prevent"), 1);   // only the Enter
  assert.equal(count(actions, "native"), GTIN.length);
  assert.equal(routes(actions).length, 1);
});

test("a class change mid-run ends the run and replays before the new one", () => {
  const { state, actions } = drive([
    key("4", T0 + 100), key("0", T0 + 112),
    key("g", T0 + 124, "select"),   // focus moved without a focusin (defensive)
  ]);
  const kinds = types(actions);
  assert.deepEqual(actions.find((a) => a.type === "replay"),
    { type: "replay", buffer: "40", cls: "text" });
  // the new run's first key: native + snap (select semantics)
  const last = actions[actions.length - 1];
  assert.equal(last.type, "native");
  assert.equal(last.snap, true);
  assert.equal(state.cls, "select");
});

test("value-class runs (number/email) replay with the value write path", () => {
  const shaped = drive(burst("12345", 12, "value"));   // GTIN digits → routed
  assert.equal(routes(shaped.actions).length, 1);
  assert.equal(count(shaped.actions, "replay"), 0);

  // "e.e.e" carries no digit: unshaped → the held chars are written back
  // through the VALUE path (no selection API on those inputs).
  const unshaped = drive(burst("e.e.e", 12, "value"));
  assert.equal(routes(unshaped.actions).length, 0);
  assert.deepEqual(unshaped.actions.find((a) => a.type === "replay"),
    { type: "replay", buffer: "e.e.e", cls: "value" });
});

// --- routing hygiene ------------------------------------------------------------

test("a route resets everything: the next key starts a fresh run", () => {
  const { state, actions } = drive([
    ...burst(GTIN),
    key("x", T0 + 500),
  ]);
  assert.equal(state.mode, "hold");
  assert.equal(state.buf, "x");
  // the post-route key is a fresh hold, not a continuation
  assert.equal(count(actions, "prevent"), GTIN.length + 2);
});

test("Enter during warm with a single buffered char writes nothing extra", () => {
  const events = [
    key("h", T0 + 100), timer(T0 + 100 + SCAN_DEFER_MS),
    key("i", T0 + 200),                 // warm, native, buf = "i"
    key("Enter", T0 + 280),             // buf < 4: native Enter, no re-write
  ];
  const { actions } = drive(events);
  // exactly one write-back total (the initial "h" hold's flush) — the
  // warm char "i" already landed natively and must not be written twice.
  assert.equal(count(actions, "replay") + count(actions, "flush"), 1);
  assert.equal(types(actions).pop(), "native");
});
