// Hold-to-confirm + double-tap guard (design refresh C3) — `node --test`.
//
// The timing decisions live in the REAL static/interaction.js, so this file
// drives an injected clock and frame pump against the shipped machine:
// threshold, early release, release-at-threshold without a frame, Escape,
// keyboard source discipline, exactly-once, latched auto-repeat, guard keying,
// in-flight async blocking and window expiry. The adapter half (pointerdown /
// keydown / keyup / auto-repeat / primary-button gating) is brace-extracted
// from the shipped static/app.js, so the wiring is tested, not re-implemented.
//
// The DOM/browser half (CSS fill, toast rendering, reduced motion) is pinned by
// pytest in tests/test_hold_guard.py — that is where static claims belong.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import HcrmInteraction from "../../static/interaction.js";

const { createHold, createGuard, HOLD_MS, GUARD_MS } = HcrmInteraction;

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const appSrc = readFileSync(join(ROOT, "static", "app.js"), "utf8");

function extractMethod(header) {
  const start = appSrc.indexOf(header);
  assert.notStrictEqual(start, -1, `definition not found: ${header}`);
  const open = start + header.length - 1;
  let depth = 0;
  for (let i = open; i < appSrc.length; i++) {
    if (appSrc[i] === "{") depth++;
    else if (appSrc[i] === "}") {
      depth--;
      if (depth === 0) return appSrc.slice(start, i + 1);
    }
  }
  assert.fail(`unbalanced braces after ${header}`);
}

/** A manual rAF: callbacks stay pending until flush(), exactly like frames. */
function fakeFrames() {
  let nextId = 1;
  const pending = new Map();
  return {
    requestFrame(callback) {
      const id = nextId++;
      pending.set(id, callback);
      return id;
    },
    cancelFrame(id) { pending.delete(id); },
    flush() {
      const callbacks = [...pending.values()];
      pending.clear();
      for (const callback of callbacks) callback();
    },
    pending() { return pending.size; },
  };
}

function harness(overrides = {}) {
  let clock = 0;
  const frames = fakeFrames();
  const log = { progress: [], states: [], fire: 0, cancel: [] };
  const hold = createHold({
    now: () => clock,
    requestFrame: frames.requestFrame,
    cancelFrame: frames.cancelFrame,
    onProgress: (value) => log.progress.push(value),
    onState: (state) => log.states.push(state),
    onFire: () => { log.fire += 1; },
    onCancel: (reason) => log.cancel.push(reason),
    ...overrides,
  });
  return {
    hold, frames, log,
    advance(ms) { clock += ms; },
    setClock(ms) { clock = ms; },
  };
}

// --- the hold-to-confirm machine -------------------------------------------

test("the timings are the design's: 700 ms hold, 600 ms guard", () => {
  assert.equal(HOLD_MS, 700);
  assert.equal(GUARD_MS, 600);
});

test("a 700 ms hold fires exactly once and reports 0..1 progress", () => {
  const h = harness();
  assert.equal(h.hold.start("pointer"), true);
  assert.equal(h.hold.phase(), "holding");
  assert.equal(h.hold.progress(), 0);

  h.advance(350);
  h.hold.tick();
  assert.equal(h.hold.progress(), 0.5);
  assert.equal(h.log.fire, 0, "halfway: nothing commits");

  h.advance(350);          // exactly 700
  h.hold.tick();
  assert.equal(h.log.fire, 1);
  assert.equal(h.log.progress.at(-1), 1);
  assert.equal(h.hold.phase(), "latched");

  // More ticks and the release must not fire a second time.
  h.hold.tick();
  h.hold.tick();
  assert.equal(h.log.fire, 1);
  h.hold.release("pointer", "pointerup");
  assert.equal(h.hold.phase(), "idle");
  assert.equal(h.hold.progress(), 0);
  assert.deepEqual(h.log.cancel, [], "a completed hold never shows the cancel hint");
});

test("an early release cancels once with its reason and kills the frame", () => {
  const h = harness();
  h.hold.start("pointer");
  h.advance(300);
  assert.equal(h.hold.release("pointer", "pointerup"), true);
  assert.equal(h.hold.phase(), "idle");
  assert.equal(h.hold.progress(), 0);
  assert.equal(h.log.fire, 0);
  assert.deepEqual(h.log.cancel, ["pointerup"]);
  assert.equal(h.frames.pending(), 0, "the scheduled rAF is cancelled");
  h.frames.flush();
  assert.equal(h.log.fire, 0, "a late frame cannot resurrect the run");
});

test("releasing at/after the threshold fires even when no frame was delivered", () => {
  const h = harness();
  h.hold.start("pointer");
  h.advance(700);
  // rAF can be throttled in a background tab: the gesture still completed.
  assert.equal(h.hold.release("pointer", "pointerup"), true);
  assert.equal(h.log.fire, 1);
  assert.deepEqual(h.log.cancel, []);
  assert.equal(h.hold.phase(), "idle");
  assert.equal(h.frames.pending(), 0);
});

test("Escape cancels a live hold but is silent after the action fired", () => {
  const h = harness();
  h.hold.start("pointer");
  h.advance(100);
  assert.equal(h.hold.cancel("escape"), true);
  assert.deepEqual(h.log.cancel, ["escape"]);
  assert.equal(h.log.fire, 0);
  assert.equal(h.hold.phase(), "idle");

  h.hold.start("pointer");
  h.advance(700);
  h.hold.tick();                       // fires, latched
  assert.equal(h.log.fire, 1);
  assert.equal(h.hold.cancel("escape"), true);
  assert.deepEqual(h.log.cancel, ["escape"], "no 'cancelled' hint after a commit");
  assert.equal(h.hold.phase(), "idle");
});

test("pointer and keyboard runs do not end each other", () => {
  const h = harness();
  assert.equal(h.hold.start("key"), true);
  h.advance(100);
  assert.equal(h.hold.release("pointer", "pointerup"), false,
    "a stray pointerup during a keyboard hold is ignored");
  assert.equal(h.hold.phase(), "holding");
  h.advance(200);
  assert.equal(h.hold.release("key", "keyup"), true);
  assert.deepEqual(h.log.cancel, ["keyup"]);
  assert.equal(h.hold.phase(), "idle");

  assert.equal(h.hold.start("pointer"), true);
  assert.equal(h.hold.release("key", "keyup"), false);
  assert.equal(h.hold.phase(), "holding");
  assert.equal(h.hold.release("pointer", "pointerup"), true);
});

test("a second start is ignored mid-run (auto-repeat cannot restart the clock)", () => {
  const h = harness();
  h.hold.start("key");
  h.advance(400);
  assert.equal(h.hold.start("key"), false, "key repeat is not a new run");
  h.advance(300);                        // 700 from the FIRST start
  h.hold.tick();
  assert.equal(h.log.fire, 1, "the original clock is what counts");

  // Latched until release: repeated keydowns after the fire are ignored.
  assert.equal(h.hold.start("key"), false);
  assert.equal(h.hold.start("pointer"), false);
  assert.equal(h.log.fire, 1);
  h.hold.release("key", "keyup");
  assert.equal(h.hold.phase(), "idle");

  // A NEW gesture after the release is legitimate and fires again.
  assert.equal(h.hold.start("key"), true);
  h.advance(700);
  h.hold.tick();
  assert.equal(h.log.fire, 2);
});

test("the rAF loop paints every step and stops at the threshold", () => {
  const h = harness();
  h.hold.start("pointer");
  for (let step = 1; step <= 5; step++) {
    h.advance(100);
    h.frames.flush();
    assert.ok(Math.abs(h.hold.progress() - step / 7) < 1e-12, `step ${step}`);
    assert.equal(h.frames.pending(), 1, "still holding: the next frame is queued");
  }
  assert.equal(h.log.fire, 0);
  h.advance(200);                        // 700 total
  h.frames.flush();
  assert.equal(h.log.fire, 1);
  assert.equal(h.hold.progress(), 1);
  assert.equal(h.frames.pending(), 0, "no frame is queued after the latch");
});

test("the duration is injectable (reusable machine, not a hard-coded timer)", () => {
  const h = harness({ duration: 2000 });
  h.hold.start("pointer");
  h.advance(700);
  h.hold.tick();
  assert.equal(h.log.fire, 0, "a longer hold threshold does not fire at 700");
  h.advance(1300);
  h.hold.tick();
  assert.equal(h.log.fire, 1);
});

// --- the shipped pointer/keyboard adapter ----------------------------------

function adapter() {
  const calls = { starts: [], releases: [], cancels: [], bindings: [], prevented: 0 };
  const fakeHold = {
    start(source) { calls.starts.push(source); return true; },
    release(source, reason) { calls.releases.push([source, reason]); return true; },
    cancel(reason) { calls.cancels.push(reason); return true; },
    active() { return false; },
  };
  const methods = {};
  for (const header of ["beginPurchaseHold(ev) {", "endPurchaseHold(reason) {",
                        "purchaseHoldKeydown(ev) {", "purchaseHoldKeyup(ev) {",
                        "cancelPurchaseHold(reason) {"]) {
    const name = header.split("(")[0];
    methods[name] = new Function(
      `"use strict"; return ({\n${extractMethod(header)}\n}).${name};`)();
  }
  const vm = {
    _hold: fakeHold,
    _holdKey: null,
    _holdEl: null,
    _bindHoldListeners(on) { calls.bindings.push(on); },
    ...methods,
  };
  return { vm, calls };
}

function keyEvent(key, extra = {}) {
  return {
    key,
    repeat: false,
    defaultPrevented: false,
    currentTarget: "button",
    preventDefault() { this.defaultPrevented = true; },
    ...extra,
  };
}

test("the SHIPPED adapter starts on a primary pointer and ignores the rest", () => {
  const { vm, calls } = adapter();
  const ev = { button: 0, isPrimary: true, currentTarget: "button" };
  vm.beginPurchaseHold(ev);
  assert.deepEqual(calls.starts, ["pointer"]);
  assert.deepEqual(calls.bindings, [true]);
  assert.equal(vm._holdEl, "button");

  vm.beginPurchaseHold({ button: 2, isPrimary: true, currentTarget: "button" });
  assert.deepEqual(calls.starts, ["pointer"], "right-click must not start a purchase");
  vm.beginPurchaseHold({ button: 0, isPrimary: false, currentTarget: "button" });
  assert.deepEqual(calls.starts, ["pointer"], "a second finger must not start one");
});

test("the SHIPPED keyboard adapter holds Enter/Space and ignores auto-repeat", () => {
  for (const key of ["Enter", " ", "Spacebar"]) {
    const { vm, calls } = adapter();
    const down = keyEvent(key);
    vm.purchaseHoldKeydown(down);
    assert.equal(down.defaultPrevented, true, "Space must not scroll");
    assert.deepEqual(calls.starts, ["key"], key === " " ? "Space starts" : key);
    assert.equal(vm._holdKey, key);
    assert.deepEqual(calls.bindings, [true]);

    const repeat = keyEvent(key, { repeat: true });
    vm.purchaseHoldKeydown(repeat);
    assert.deepEqual(calls.starts, ["key"], "key repeat is not a second run");
    assert.equal(repeat.defaultPrevented, true);

    const up = keyEvent(key);
    vm.purchaseHoldKeyup(up);
    assert.deepEqual(calls.releases, [["key", "keyup"]]);
    assert.equal(vm._holdKey, null);
  }
});

test("the SHIPPED keyboard adapter ignores a different key's release", () => {
  const { vm, calls } = adapter();
  vm.purchaseHoldKeydown(keyEvent("Enter"));
  const other = keyEvent(" ");
  vm.purchaseHoldKeyup(other);
  assert.deepEqual(calls.releases, [], "Enter is still held");
  assert.equal(vm._holdKey, "Enter");
  vm.purchaseHoldKeyup(keyEvent("Enter"));
  assert.deepEqual(calls.releases, [["key", "keyup"]]);
});

test("the SHIPPED adapter forwards the cancel reason and the pointer release source", () => {
  const { vm, calls } = adapter();
  vm.cancelPurchaseHold("escape");
  assert.deepEqual(calls.cancels, ["escape"]);
  assert.deepEqual(calls.bindings, [false]);
  vm.endPurchaseHold("pointerleave");
  assert.deepEqual(calls.releases, [["pointer", "pointerleave"]]);
});

// --- the double-tap guard ---------------------------------------------------

function guardHarness(ms) {
  let clock = 0;
  const guard = createGuard({ now: () => clock, ...(ms === undefined ? {} : { ms }) });
  return {
    guard, clock: () => clock,
    advance(value) { clock += value; },
  };
}

test("the same key is ignored inside the 600 ms window, allowed at the boundary", () => {
  const g = guardHarness();
  const calls = [];
  const first = g.guard.guarded("cart:add:1", () => { calls.push("first"); return "ok"; });
  assert.equal(first, "ok", "the first call runs and its return value passes through");
  assert.equal(g.guard.guarded("cart:add:1", () => { calls.push("second"); return "no"; }),
    undefined, "the second tap is swallowed");
  assert.deepEqual(calls, ["first"]);

  g.advance(599);
  g.guard.guarded("cart:add:1", () => calls.push("at599"));
  assert.deepEqual(calls, ["first"]);

  g.advance(1);                             // exactly 600: outside the window
  g.guard.guarded("cart:add:1", () => calls.push("at600"));
  assert.deepEqual(calls, ["first", "at600"]);
});

test("different keys never cross-block", () => {
  const g = guardHarness();
  const calls = [];
  g.guard.guarded("item:delete:1", () => calls.push("item1"));
  g.guard.guarded("item:delete:2", () => calls.push("item2"));
  g.guard.guarded("member:delete:1", () => calls.push("member1"));
  assert.deepEqual(calls, ["item1", "item2", "member1"]);
});

test("a slow action blocks its key until it settles, even past the window", async () => {
  const g = guardHarness();
  let resolve;
  const calls = [];
  const pending = g.guard.guarded("purchase:confirm", () => {
    calls.push("start");
    return new Promise((done) => { resolve = done; });
  });
  g.advance(5000);                          // far past 600 ms
  g.guard.guarded("purchase:confirm", () => calls.push("race"));
  assert.deepEqual(calls, ["start"], "in flight: no second request can race the first");
  resolve("done");
  assert.equal(await pending, "done");
  await Promise.resolve();                  // let the guard's settle handler run
  g.guard.guarded("purchase:confirm", () => calls.push("after"));
  assert.deepEqual(calls, ["start", "after"], "after the settle + window: allowed");
});

test("a rejected async action clears the in-flight flag", async () => {
  const g = guardHarness(0);
  let reject;
  const calls = [];
  const pending = g.guard.guarded("checkout:go", () => new Promise((_, fail) => {
    calls.push("start");
    reject = fail;
  }));
  reject(new Error("network"));
  await pending.catch(() => {});
  await Promise.resolve();
  g.guard.guarded("checkout:go", () => calls.push("retry"));
  assert.deepEqual(calls, ["start", "retry"]);
});

test("a synchronous throw clears the in-flight flag and still propagates", () => {
  const g = guardHarness(0);
  assert.throws(() => g.guard.guarded("cart:clear", () => {
    throw new Error("boom");
  }), /boom/);
  const calls = [];
  g.guard.guarded("cart:clear", () => calls.push("retry"));
  assert.deepEqual(calls, ["retry"]);
});

test("a per-call window overrides the default and keys are string-coerced", () => {
  const g = guardHarness();
  const calls = [];
  g.guard.guarded(42, () => calls.push("a"), 100);
  g.advance(100);
  g.guard.guarded("42", () => calls.push("b"));   // same key, its own window
  assert.deepEqual(calls, ["a", "b"], "42 and '42' are one key; window expired");
  g.guard.guarded("42", () => calls.push("c"));
  assert.deepEqual(calls, ["a", "b"], "the default 600 ms window now applies");
});
