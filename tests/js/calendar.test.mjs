// Slice C behaviour tests — the spend calendar's labels, `node --test`.
//
// The labels are the slice's copy surface: the 7 weekday and 12 month names
// come from the CSV (date.dow.*/date.month.*) so the Danish grid stays
// compact, the day tooltip is a .one/.other pair with the money injected as a
// placeholder, and the month total switches between "Spent this month" and
// "Spent in {month}". The methods are eval'd out of the REAL static/app.js
// (the loadmore.test.mjs idiom), against the REAL compiled bundle — no mocks
// of the copy.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import MESSAGES from "../../static/i18n.messages.js";
import HcrmI18n from "../../static/i18n.js";

globalThis.HCRM_MESSAGES = MESSAGES;

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const appSrc = readFileSync(join(ROOT, "static", "app.js"), "utf8");

const COMPUTED_OPEN = appSrc.indexOf("\n  computed: {\n"), WATCH_OPEN = appSrc.indexOf("\n  watch: {\n");
const METHODS_OPEN = appSrc.indexOf("\n  methods: {\n");
const methodsOf = (...h) => new Function(`"use strict"; return ({\n${h.map(extractMethod).join(",\n")}\n});`)();
function navigationObject({ year, month }) {
  const calls = [];
  const state = { calYear: year, calMonth: month, ...methodsOf("shiftCalendarMonth(delta) {"),
    loadCalendar(y, m) { calls.push({ year: y, month: m }); return Promise.resolve({ year: y, month: m }); } };
  return { state, calls };
}

function extractMethod(header) {
  // Brace-matched extraction: the header is the definition text ending in " {"
  // so a call site can never match.
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
  throw new Error(`unbalanced braces after ${header}`);
}

function calendarObject() {
  const headers = [
    "t(id, vars) {", "tp(base, n, vars) {", "formatPrice(cents) {",
    "formatLang() {", "calendarWeekdays() {", "calendarMonthLabel() {",
    "calendarMonthLongLabel() {", "calendarTotalLabel() {",
    "calendarDayTitle(key) {", "calendarDay(key) {",
  ];
  const body = headers.map(extractMethod).join(",\n");
  const methods = new Function("HcrmI18n", `"use strict"; return ({\n${body}\n});`)(HcrmI18n);
  const state = {
    lang: "en",
    calYear: 2026,
    calMonth: 3,   // March
    calIsCurrent: false,
    calDays: {
      "2026-03-05": { count: 2, cents: 123450 },
      "2026-03-06": { count: 1, cents: 5000 },
    },
    ...methods,
  };
  // Computeds are unwrapped in the real app (`this.calendarMonthLongLabel` is
  // a VALUE there); this getter exposes the shipped body's value, so the
  // total-label assertion exercises the real call path.
  Object.defineProperty(state, "calendarMonthLongLabel", {
    get() { return methods.calendarMonthLongLabel.call(state); },
  });
  return state;
}

test("weekday labels are CSV rows and stay narrow in Danish", () => {
  const en = calendarObject();
  const da = calendarObject();
  da.lang = "da";
  assert.deepEqual(en.calendarWeekdays(),
    ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]);
  assert.deepEqual(da.calendarWeekdays(),
    ["ma", "ti", "on", "to", "fr", "lø", "sø"]);
  assert.ok(da.calendarWeekdays().every((label) => label.length <= 2),
    "the Danish weekday header must fit the 7-column grid (no Intl 'man.')");
});

test("the month title is the CSV month name plus the numeric year", () => {
  const en = calendarObject();
  const da = calendarObject();
  da.lang = "da";
  assert.equal(en.calendarMonthLabel(), "March 2026");
  assert.equal(da.calendarMonthLabel(), "mar 2026");
  // Before the first calendar load the template falls back to calendar.title.
  const empty = calendarObject();
  empty.calYear = 0;
  assert.equal(empty.calendarMonthLabel(), "");
});

test("prose uses the long month names; the grid header keeps the shorts", () => {
  const en = calendarObject();
  const da = calendarObject();
  da.lang = "da";
  assert.equal(en.calendarMonthLongLabel, "March 2026");
  assert.equal(da.calendarMonthLongLabel, "marts 2026");
  assert.equal(da.calendarMonthLabel(), "mar 2026",
    "the compact header keeps date.month.* (shorts) for the 7-column grid");
});

test("the calendar total label is prose: long month, or 'this month'", () => {
  const da = calendarObject();
  da.lang = "da";
  da.calIsCurrent = true;
  assert.equal(da.calendarTotalLabel(), "Brugt denne måned");
  da.calIsCurrent = false;
  assert.equal(da.calendarTotalLabel(), "Brugt i marts 2026");
  assert.notEqual(da.calendarTotalLabel(), "Brugt i mar 2026",
    "a sentence must not read the grid short (slice-C review)");
  const en = calendarObject();
  assert.equal(en.calendarTotalLabel(), "Spent in March 2026");
});

test("the day tooltip is a plural pair with the money as a placeholder", () => {
  const en = calendarObject();
  const da = calendarObject();
  da.lang = "da";
  assert.equal(en.calendarDayTitle("2026-03-05"), "2 purchases · 1.234,50 DKK");
  assert.equal(en.calendarDayTitle("2026-03-06"), "1 purchase · 50,00 DKK");
  assert.equal(da.calendarDayTitle("2026-03-05"), "2 køb · 1.234,50 DKK");
  assert.equal(da.calendarDayTitle("2026-03-06"), "1 køb · 50,00 DKK");
  assert.equal(en.calendarDayTitle("2026-03-07"), "",
    "a day without a purchase has no dot and no tooltip");
});

test("a language flip changes the labels at paint time (no reload)", () => {
  const state = calendarObject();
  assert.equal(state.calendarMonthLabel(), "March 2026");
  assert.equal(state.calendarWeekdays()[5], "Sat");
  state.lang = "da";
  assert.equal(state.calendarMonthLabel(), "mar 2026");
  assert.equal(state.calendarMonthLongLabel, "marts 2026");
  assert.equal(state.calendarWeekdays()[5], "lø");
  assert.equal(state.calendarDayTitle("2026-03-05"), "2 køb · 1.234,50 DKK");
});

test("the calendar tooltip is a method, not a computed (blank-SPA regression)", () => {
  const at = appSrc.indexOf("calendarDayTitle(key) {");
  assert.ok(COMPUTED_OPEN !== -1 && WATCH_OPEN !== -1 && METHODS_OPEN !== -1, "options blocks not found");
  assert.ok(at > METHODS_OPEN && !(COMPUTED_OPEN < at && at < WATCH_OPEN),
    "calendarDayTitle must sit in `methods:`, never between `computed: {` and `watch: {`");
});

test("shiftCalendarMonth returns the load, so navigation is awaitable", async () => {
  const { state } = navigationObject({ year: 2026, month: 12 });
  const pending = state.shiftCalendarMonth(1);
  assert.equal(typeof pending.then, "function", "must return loadCalendar's promise");
  await pending;
});

test("shiftCalendarMonth rolls the year over in both directions", async () => {
  const cases = [   // from, delta, want
    [{ year: 2026, month: 12 }, 1, { year: 2027, month: 1 }],    // December + 1 = January next year
    [{ year: 2026, month: 1 }, -1, { year: 2025, month: 12 }],   // January - 1 = December last year
    [{ year: 2026, month: 3 }, -1, { year: 2026, month: 2 }],    // in-month shifts keep the year
    [{ year: 2026, month: 3 }, 1, { year: 2026, month: 4 }]];
  for (const [from, delta, want] of cases) {
    const { state, calls } = navigationObject(from);
    assert.deepEqual(await state.shiftCalendarMonth(delta).then(() => calls), [want]);
  }
});

test("calendarDay refuses a non-string key instead of coercing a Vue proxy", () => {
  const thrower = { toString() { throw new TypeError("Cannot convert object to primitive value"); } };
  const state = { calDays: { "2026-03-05": { cents: 1200, count: 1 } }, ...methodsOf("calendarDay(key) {") };
  assert.deepEqual([state.calendarDay(thrower), state.calendarDay(undefined)], [null, null],
    "a proxy-like or missing key answers null, never throws");
  assert.deepEqual(state.calendarDay("2026-03-05"), { cents: 1200, count: 1 });
});
