// Money formatter tests (Danish/DKK roadmap, item 1) — `node --test tests/js/`.
//
// static/i18n.js is pure: no DOM, no locale services, no timers. These
// tests pin the DKK contract byte-exactly (the harnesses here are
// byte/regex exact — that is why the formatter is hand-rolled integer
// arithmetic at all), then brace-extract the REAL static/app.js helpers
// (the loadmore.test.mjs discipline) to prove the delegation and the
// CSV/PDF split actually execute rather than merely appear in the source.
// The mocked-jsPDF test pins the export's geometry: subtitle text + y,
// startY, the formatted price cell, and that the no-price path keeps
// today's exact layout.

import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import HcrmI18n from "../../static/i18n.js";
import MESSAGES from "../../static/i18n.messages.js";

const { money, MONEY_CODE } = HcrmI18n;

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = readFileSync(join(ROOT, "static", "app.js"), "utf8");

/** Slice E: the export bodies read their copy through the SHIPPED runtime, so
 *  the assertions below see the real CSV cells (not a test-only copy). The
 *  bundle is published only for the export tests: the money contract above
 *  runs on the formatter's own DKK default, which is the point of that pin. */
function useBundle(t) {
  globalThis.HCRM_MESSAGES = MESSAGES;
  t.after(() => { delete globalThis.HCRM_MESSAGES; });
}
const enT = (id, vars) => HcrmI18n.translate(id, vars, "en");
const enTp = (base, n, vars) => HcrmI18n.plural(base, n, vars, "en");

// --- the DKK contract ------------------------------------------------------

const CONTRACT = [
  [0, "0,00 DKK"],
  [5, "0,05 DKK"],
  [-5, "-0,05 DKK"],
  [99, "0,99 DKK"],
  [100, "1,00 DKK"],
  [999, "9,99 DKK"],
  [1005, "10,05 DKK"],
  [9990, "99,90 DKK"],
  [99999, "999,99 DKK"],
  [100000, "1.000,00 DKK"],
  [123450, "1.234,50 DKK"],
  [123456789, "1.234.567,89 DKK"],
  [-1230, "-12,30 DKK"],
  [-123450, "-1.234,50 DKK"],
  [1234.4, "12,34 DKK"],
  [1234.5, "12,35 DKK"],
  [-1234.4, "-12,34 DKK"],
  [-1234.5, "-12,35 DKK"],
  [0.5, "0,01 DKK"],
  [-0.5, "-0,01 DKK"],
  [-0.4, "0,00 DKK"],
  [-0, "0,00 DKK"],
  ["123450", "1.234,50 DKK"],
];

test("money: the DKK contract is byte-exact", () => {
  for (const [input, expected] of CONTRACT) {
    assert.equal(money(input), expected, `money(${String(input)})`);
  }
});

test("money: missing and non-finite input renders 0,00 DKK, never NaN", () => {
  // The old formatter's `(cents || 0)` leaked "NaN" for truthy garbage;
  // the contract is now total: anything unreadable renders zero.
  for (const input of [undefined, null, NaN, Infinity, -Infinity, "", "x", {}]) {
    assert.equal(money(input), "0,00 DKK", `money(${String(input)})`);
  }
});

test("money: rounding is symmetric, half away from zero", () => {
  // Fractional cents reach the formatter from the analysis averages
  // (priceMean/priceMedian/priceStd). Half-øre rounds away from zero in
  // BOTH directions — the half-expand behaviour of the formatter this
  // replaces, without its negative-half asymmetry.
  assert.equal(money(1234.5), "12,35 DKK");
  assert.equal(money(-1234.5), "-12,35 DKK");
  assert.equal(money(0.5), "0,01 DKK");
  assert.equal(money(-0.5), "-0,01 DKK");
  // A value that rounds to zero is never negative.
  assert.equal(money(-0.4), "0,00 DKK");
  assert.equal(money(-0), "0,00 DKK");
});

test("money: ASCII separator, bare code last, no NBSP-class codepoint", () => {
  const outputs = [money(0), money(5), money(-123450), money(123456789), money("123450")];
  for (const out of outputs) {
    assert.match(out, /^-?\d{1,3}(\.\d{3})*,\d{2} DKK$/, `shape: ${out}`);
    assert.ok(!/[\u00a0\u202f\u2009]/.test(out), `NBSP-class codepoint in ${out}`);
    assert.ok(out.endsWith(" " + MONEY_CODE), `suffix: ${out}`);
    assert.equal(out.split(" ").length, 2, `exactly one space: ${out}`);
    assert.ok(!/^DKK /.test(out), `never the prefix form: ${out}`);
    assert.ok(/^[\x20-\x7e]*$/.test(out), `pure ASCII: ${out}`);
  }
});

test("money: the currency-code injection hook (item 2's money.code row)", () => {
  assert.equal(MONEY_CODE, "DKK");
  assert.equal(money(123450, "kr."), "1.234,50 kr.");
  assert.equal(money(123450, ""), "1.234,50 DKK");
});

test("money: absurd magnitudes clamp to plain digits, never exponent notation", () => {
  assert.equal(money(1e21), "90.071.992.547.409,91 DKK");
  assert.equal(money(-1e21), "-90.071.992.547.409,91 DKK");
  for (const input of [1e23, -1e23, 1e30]) {
    const out = money(input).replace("DKK", "");
    assert.ok(!/e/i.test(out), `exponent notation leaked for ${input}: ${out}`);
  }
});

test("money: CommonJS require and ESM import expose the same module", () => {
  const require_ = createRequire(import.meta.url);
  const cjs = require_("../../static/i18n.js");
  assert.equal(cjs.money, money);
  assert.equal(cjs.MONEY_CODE, "DKK");
});

// --- the real static/app.js wiring ------------------------------------------

function extractMethod(header) {
  // Brace-matched extraction (the loadmore.test.mjs discipline): the header
  // is the DEFINITION text ending in " {" so a call site can never match.
  const start = src.indexOf(header);
  assert.notStrictEqual(start, -1, `definition not found: ${header}`);
  const open = start + header.length - 1;
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  assert.fail(`unbalanced braces after ${header}`);
}

function appMethods(...headers) {
  const body = headers.map(extractMethod).join(",\n");
  return new Function("HcrmI18n", `"use strict"; return ({\n${body}\n});`)(HcrmI18n);
}

test("app.js: formatPrice delegates and the CSV/PDF split executes", () => {
  const m = appMethods(
    "formatPrice(cents) {",
    "exportValue(row, key) {",
    "exportPdfValue(row, key) {",
  );
  const row = { name: "Widget", price_cents: 123450, stock: 3 };
  assert.equal(m.formatPrice(123450), "1.234,50 DKK");
  assert.equal(m.formatPrice(undefined), "0,00 DKK");
  assert.equal(m.formatPrice(-1230), "-12,30 DKK");
  // The CSV cell stays machine-numeric; the PDF cell carries the DKK form.
  assert.equal(m.exportValue(row, "price_cents"), "1234.50");
  assert.equal(m.exportPdfValue(row, "price_cents"), "1.234,50 DKK");
  assert.equal(m.exportPdfValue(row, "name"), "Widget");
  assert.equal(m.exportPdfValue(row, "stock"), "3");
});

test("app.js: exportPDF writes the subtitle, formatted cells and geometry", (t) => {
  const texts = [];
  const tables = [];
  const saves = [];
  const doc = {
    setFontSize() {},
    text(str, x, y) { texts.push([str, x, y]); },
    autoTable(opts) { tables.push(opts); },
    save(name) { saves.push(name); },
  };
  const originalWindow = globalThis.window;
  globalThis.window = { jspdf: { jsPDF: function FakeJsPDF() { return doc; } } };
  t.after(() => {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  });

  useBundle(t);
  const m = appMethods(
    "formatPrice(cents) {",
    "formatLang() {",
    "exportValue(row, key) {",
    "exportPdfValue(row, key) {",
    "msg(value) {",
    "_counts(vars) {",
    "exportPDF() {",
  );
  const row = { name: "Widget", price_cents: 123450, stock: 3 };
  const toasts = [];
  Object.assign(m, {
    lang: "en", t: enT, tp: enTp,
    showToast: (msg) => toasts.push(msg),
  });

  // With a price column: title, summary, the currency note, the moved
  // table top, and formatted money in the body.
  m.exportRows = [row];
  m.exportCols = [
    { key: "name", label: "Name" },
    { key: "price_cents", label: "Price (DKK)" },
  ];
  m.exportPDF();
  assert.equal(texts.length, 3);
  assert.deepEqual(texts[0], [MESSAGES.en["export.pdf_title"], 14, 15]);
  assert.equal(texts[1][1], 14, "summary x");
  assert.equal(texts[1][2], 22, "summary y (unchanged)");
  // The summary is the row pair, not a "row(s)" idiom: 1 row, 2 columns.
  assert.match(texts[1][0], /^1 row × 2 columns — /);
  assert.deepEqual(texts[2], [MESSAGES.en["export.pdf_prices"], 14, 28]);
  assert.equal(tables.length, 1);
  assert.equal(tables[0].startY, 33);
  assert.deepEqual(tables[0].head, [["Name", "Price (DKK)"]]);
  assert.deepEqual(tables[0].body, [["Widget", "1.234,50 DKK"]]);
  assert.deepEqual(saves, ["items-export.pdf"]);
  // The toast is a descriptor with a count PER dimension (slice E): the two
  // phrases are resolved at paint time, so an open toast flips with the flag.
  assert.deepEqual(toasts, [{
    key: "export.done",
    vars: {
      rows: { key: "common.rows", plural: 1 },
      cols: { key: "common.cols", plural: 2 },
      format: "PDF",
    },
  }]);
  assert.equal(m.msg(toasts[0]), "Exported 1 row × 2 columns as PDF");

  // Without a price column: no currency claim and today's exact layout.
  texts.length = 0; tables.length = 0; saves.length = 0; toasts.length = 0;
  m.exportCols = [
    { key: "name", label: "Name" },
    { key: "stock", label: "Stock" },
  ];
  m.exportPDF();
  assert.equal(texts.length, 2);
  assert.ok(!texts.some(([s]) => s === MESSAGES.en["export.pdf_prices"]),
            "no currency note without a price column");
  assert.equal(tables[0].startY, 27);
  assert.deepEqual(tables[0].body, [["Widget", "3"]]);
  assert.deepEqual(saves, ["items-export.pdf"]);
});

test("app.js: exportCSV keeps cells numeric while the header names the currency", (t) => {
  // The other half of the export decision (item 1's whole point for CSVs):
  // the header states the currency, the cells stay summable, the BOM and the
  // formula-injection guard survive. Pinned by RUNNING exportCSV with a
  // recorded downloadBlob rather than by grepping its body.
  useBundle(t);
  const m = appMethods(
    "formatPrice(cents) {",
    "exportValue(row, key) {",
    "csvEscape(value) {",
    "msg(value) {",
    "_counts(vars) {",
    "exportCSV() {",
  );
  const downloads = [];
  const toasts = [];
  Object.assign(m, {
    lang: "en", t: enT, tp: enTp,
    downloadBlob: (content, filename, type) => downloads.push({ content, filename, type }),
    showToast: (msg) => toasts.push(msg),
  });
  m.exportRows = [
    { name: "Widget", price_cents: 123450, stock: 3 },
    { name: "=SUM(1,2)", price_cents: 5, stock: 0 },   // formula-injection guard
  ];
  m.exportCols = [
    { key: "name", label: "Name" },
    { key: "price_cents", label: "Price (DKK)" },
  ];

  m.exportCSV();

  assert.equal(downloads.length, 1);
  const { content, filename, type } = downloads[0];
  assert.equal(filename, "items-export.csv");
  assert.equal(type, "text/csv;charset=utf-8");
  assert.ok(content.startsWith("\ufeff"), "the UTF-8 BOM prefix must stay");
  assert.deepEqual(content.slice(1).split("\n"), [
    "Name,Price (DKK)",
    "Widget,1234.50",
    "\"'=SUM(1,2)\",0.05",
  ]);
  assert.equal(m.msg(toasts[0]), "Exported 2 rows × 2 columns as CSV");
});
