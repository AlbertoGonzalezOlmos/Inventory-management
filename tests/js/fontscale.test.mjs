// Adjustable font size (design refresh C2) — `node --test tests/js/`.
//
// The grid, the clamp, the persistence guards and the root px are pure claims,
// so they are driven against the REAL static/interaction.js. The Vue wiring is
// not re-implemented either: the shipped `smallerFont`/`largerFont` methods,
// the `fontScale` watcher, the end-guard computeds and the percentage hint are
// brace-extracted from static/app.js (the langtoggle.test.mjs idiom) and run
// against a fake document/storage. The hint is asserted through the REAL
// compiled bundle, so a CSV change that drops {percent} fails here as well as
// in the pytest copy pins.
//
// What is NOT here: the pre-mount boot order and the "no writer outside the
// watcher" wiring are static claims — pytest owns those (tests/test_font_scale.py).

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import MESSAGES from "../../static/i18n.messages.js";
import HcrmI18n from "../../static/i18n.js";
import HcrmInteraction from "../../static/interaction.js";

globalThis.HCRM_MESSAGES = MESSAGES;

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const appSrc = readFileSync(join(ROOT, "static", "app.js"), "utf8");

const {
  FONT_SCALE_KEY, FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_STEP,
  FONT_SCALE_DEFAULT, ROOT_FONT_PX,
  normaliseScale, readScale, writeScale, stepScale, rootPx, applyScale,
} = HcrmInteraction;

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

/** The SHIPPED method/computed bodies, compiled with their module-scope deps. */
function appObject(...headers) {
  const body = headers.map(extractMethod).join(",\n");
  return new Function(
    "HcrmI18n", "HcrmInteraction",
    `"use strict"; return ({\n${body}\n});`,
  )(HcrmI18n, HcrmInteraction);
}

/** A localStorage stand-in that records every write. */
function fakeStore(value = null) {
  return {
    writes: [],
    getItem(key) {
      assert.equal(key, FONT_SCALE_KEY);
      return value;
    },
    setItem(key, v) {
      this.writes.push([key, v]);
    },
  };
}

const throwingStore = {
  getItem() { throw new Error("storage denied"); },
  setItem() { throw new Error("storage denied"); },
};

/** A minimal document whose root font size can be read back. */
const fakeDocument = () => ({ documentElement: { style: {} } });

// --- the pure helper --------------------------------------------------------

test("the spec's scale is pinned: 0.85–1.35, step 0.1, default 1, key hcrm_font_scale", () => {
  assert.equal(FONT_SCALE_KEY, "hcrm_font_scale");
  assert.equal(FONT_SCALE_MIN, 0.85);
  assert.equal(FONT_SCALE_MAX, 1.35);
  assert.equal(FONT_SCALE_STEP, 0.1);
  assert.equal(FONT_SCALE_DEFAULT, 1);
  assert.equal(ROOT_FONT_PX, 16);
});

test("normaliseScale accepts the range and ignores everything non-numeric or out of range", () => {
  // Accepted: numbers and numeric strings inside the bounds.
  assert.equal(normaliseScale(0.85), 0.85);
  assert.equal(normaliseScale(1), 1);
  assert.equal(normaliseScale(1.35), 1.35);
  assert.equal(normaliseScale("1.2"), 1.2);
  assert.equal(normaliseScale(" 1.15 "), 1.15, "a stored value may carry whitespace");
  assert.equal(normaliseScale(0.9000000000000001), 0.9, "float drift is rounded to 2 dp");

  // Ignored: blanks, junk, non-numbers (Number(true) would be 1), NaN/Infinity,
  // and both sides of the range.
  for (const raw of [null, undefined, "", "   ", "abc", "1.2abc", NaN,
    Infinity, -Infinity, 0.84, 1.36, 0, 100, -1, true, false, {}, [], [1]]) {
    assert.equal(normaliseScale(raw), null, `must ignore ${String(raw)}`);
  }
});

test("readScale returns the stored value, else the default — and never writes", () => {
  const stored = fakeStore("1.2");
  assert.equal(readScale(stored), 1.2);
  assert.deepEqual(stored.writes, [], "a read is never a write (first visit stays unwritten)");

  const empty = fakeStore(null);
  assert.equal(readScale(empty), FONT_SCALE_DEFAULT);
  assert.deepEqual(empty.writes, []);

  for (const junk of ["abc", "", "1.4", "0.5", "1.2abc"]) {
    assert.equal(readScale(fakeStore(junk)), FONT_SCALE_DEFAULT,
      `stored ${JSON.stringify(junk)} must be ignored`);
  }
  assert.equal(readScale(throwingStore), FONT_SCALE_DEFAULT,
    "a storage-blocked kiosk boots at 100 %, never throws");
});

test("writeScale stores the canonical value only, and swallows a blocked store", () => {
  const store = fakeStore(null);
  assert.equal(writeScale(store, 1.2), true);
  assert.deepEqual(store.writes, [[FONT_SCALE_KEY, "1.2"]]);

  assert.equal(writeScale(store, 1.2000000000000002), true, "drift is normalised");
  assert.deepEqual(store.writes.at(-1), [FONT_SCALE_KEY, "1.2"]);

  const before = store.writes.length;
  for (const junk of [null, "abc", 0.5, 1.4, NaN, true, {}]) {
    assert.equal(writeScale(store, junk), false, `must refuse ${String(junk)}`);
  }
  assert.equal(store.writes.length, before, "an invalid value never reaches storage");

  assert.equal(writeScale(throwingStore, 1.1), false);
  assert.equal(writeScale(null, 1.1), false, "no store at hand: report, never throw");
});

test("stepScale: 0.1 steps anchored at 1, clamped at the 0.85/1.35 end stops", () => {
  // The full up chain from the default: 1 -> 1.1 -> 1.2 -> 1.3 -> 1.35 (clamp).
  let up = 1;
  const ups = [];
  for (let i = 0; i < 6; i++) { up = stepScale(up, 1); ups.push(up); }
  assert.deepEqual(ups, [1.1, 1.2, 1.3, 1.35, 1.35, 1.35]);

  // The full down chain: 1 -> 0.9 -> 0.85 (clamp, because 0.8 is out of range).
  let down = 1;
  const downs = [];
  for (let i = 0; i < 6; i++) { down = stepScale(down, -1); downs.push(down); }
  assert.deepEqual(downs, [0.9, 0.85, 0.85, 0.85, 0.85, 0.85]);

  assert.equal(stepScale("1.2", 1), 1.3, "a stored string steps like a number");
  assert.equal(stepScale(1.35, 1), 1.35);
  assert.equal(stepScale(0.85, -1), 0.85);
  // An end stop is OFF the 1-anchored grid, so stepping inward snaps back to
  // the nearest grid point (not to 1.25/0.95, which would make the top/bottom
  // oscillate and 100 % unreachable from the ceiling).
  assert.equal(stepScale(1.35, -1), 1.3);
  assert.equal(stepScale(1.3, 1), 1.35);
  assert.equal(stepScale(0.85, 1), 0.9);
  assert.equal(stepScale(0.9, -1), 0.85);
  // An in-range off-grid stored value lands on the neighbouring grid points.
  assert.equal(stepScale(1.03, 1), 1.1);
  assert.equal(stepScale(1.03, -1), 1);
  assert.equal(stepScale("junk", 1), 1.1, "junk starts from the default");
  assert.equal(stepScale(1.1, 0), 1.1, "direction 0 keeps the value");
  assert.equal(stepScale(1.1, "x"), 1.1, "a non-finite direction keeps the value");
});

test("rootPx/applyScale: 16px * scale on the ROOT, junk is 100 %, no doc is safe", () => {
  assert.equal(rootPx(1), 16);
  assert.equal(rootPx(0.85), 13.6);
  assert.equal(rootPx(1.35), 21.6);
  assert.equal(rootPx(1.15), 18.4, "no binary-float tail in the px string");
  assert.equal(rootPx("junk"), 16);

  const doc = fakeDocument();
  assert.equal(applyScale(0.9, doc), 0.9);
  assert.equal(doc.documentElement.style.fontSize, "14.4px");

  assert.equal(applyScale(1.2, doc), 1.2);
  assert.equal(doc.documentElement.style.fontSize, "19.2px", "idempotent re-apply");

  assert.equal(applyScale("junk", doc), 1);
  assert.equal(doc.documentElement.style.fontSize, "16px",
    "junk applies the default rather than leaving the old size on screen");

  assert.equal(applyScale(1.1, null), 1.1, "no document (node) is not an error");
});

// --- the shipped app.js wiring ---------------------------------------------

test("the SHIPPED stepper methods walk the helper's grid and stop at the ends", () => {
  const m = appObject("smallerFont() {", "largerFont() {");
  const vm = { fontScale: 1, ...m };

  vm.largerFont();
  assert.equal(vm.fontScale, 1.1);
  vm.largerFont();
  assert.equal(vm.fontScale, 1.2);
  vm.largerFont();
  assert.equal(vm.fontScale, 1.3);
  vm.largerFont();
  assert.equal(vm.fontScale, 1.35);
  vm.largerFont();
  assert.equal(vm.fontScale, 1.35, "an end press is a no-op, not an overflow");

  vm.smallerFont();
  assert.equal(vm.fontScale, 1.3);
  for (let i = 0; i < 10; i++) vm.smallerFont();
  assert.equal(vm.fontScale, 0.85, "the floor is 85 %, never 80 % or lower");
});

test("the SHIPPED end guards disable exactly at the stops", () => {
  const m = appObject("canShrinkFont() {", "canGrowFont() {");
  const state = (fontScale) => ({ fontScale, ...m });
  assert.equal(state(1).canShrinkFont(), true);
  assert.equal(state(1).canGrowFont(), true);
  assert.equal(state(0.9).canShrinkFont(), true);
  assert.equal(state(0.85).canShrinkFont(), false, "at the floor: shrink disabled");
  assert.equal(state(0.85).canGrowFont(), true);
  assert.equal(state(1.3).canGrowFont(), true);
  assert.equal(state(1.35).canGrowFont(), false, "at the ceiling: grow disabled");
  assert.equal(state(1.35).canShrinkFont(), true);
});

test("the SHIPPED watcher applies then persists, and applies even when storage throws", (t) => {
  const watcher = new Function(
    "HcrmInteraction",
    `"use strict"; return ({\n${extractMethod("fontScale(next) {")}\n}).fontScale;`,
  )(HcrmInteraction);

  const savedStorage = globalThis.localStorage;
  const savedDocument = globalThis.document;
  t.after(() => {
    if (savedStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = savedStorage;
    if (savedDocument === undefined) delete globalThis.document;
    else globalThis.document = savedDocument;
  });

  const store = fakeStore(null);
  const doc = fakeDocument();
  globalThis.localStorage = store;
  globalThis.document = doc;

  watcher.call({}, 1.2);
  assert.equal(doc.documentElement.style.fontSize, "19.2px", "apply happens first");
  assert.deepEqual(store.writes, [[FONT_SCALE_KEY, "1.2"]], "then the write");

  watcher.call({}, 0.9);
  assert.equal(doc.documentElement.style.fontSize, "14.4px");
  assert.deepEqual(store.writes.at(-1), [FONT_SCALE_KEY, "0.9"]);

  // A blocked store must not cost the user the size they just chose.
  globalThis.localStorage = throwingStore;
  watcher.call({}, 1.35);
  assert.equal(doc.documentElement.style.fontSize, "21.6px",
    "the session still scales on a storage-blocked kiosk");
});

test("the SHIPPED hint renders the live percentage in both languages", () => {
  const m = appObject("t(id, vars) {", "fontScalePercent() {", "fontScaleHint() {");
  const state = { lang: "en", fontScale: 1, ...m };
  Object.defineProperty(state, "fontScalePercent", {
    get() { return m.fontScalePercent.call(state); },
  });
  Object.defineProperty(state, "fontScaleHint", {
    get() { return m.fontScaleHint.call(state); },
  });

  assert.equal(state.fontScaleHint, "Text size: 100%");
  assert.equal(state.fontScaleHint,
    MESSAGES.en["ui.font_scale_hint"].replace("{percent}", "100"),
    "the percentage is interpolated from the CSV row, not concatenated");

  state.lang = "da";
  assert.equal(state.fontScaleHint, "Tekststørrelse: 100 %",
    "the Danish row puts the space before the percent sign");

  state.fontScale = 1.35;
  assert.equal(state.fontScaleHint, "Tekststørrelse: 135 %");
  state.lang = "en";
  assert.equal(state.fontScaleHint, "Text size: 135%");
});
