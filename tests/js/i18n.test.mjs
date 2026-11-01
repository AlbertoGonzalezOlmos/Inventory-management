// i18n runtime tests (Danish/DKK roadmap, item 2) — `node --test tests/js/`.
//
// Two real files, no mocks of the module under test: `static/i18n.messages.js` (the
// bytes `scripts/build_i18n.py` emits from i18n/strings.csv) is loaded first and
// published as the global the runtime reads, exactly as `static/index.html` orders the
// two <script> tags. Each `node --test` file runs in its own process, so
// tests/js/money.test.mjs — which imports only the runtime — still exercises the "DKK"
// fallback that matters when the bundle is absent.
//
// What is pinned here is behaviour the browser gate cannot see: the fallback chain, the
// missing-key marker plus its single console.error (the string is in ui_check's
// FAIL_PATTERNS, so this log really does red the gate), single-pass interpolation,
// plural selection, language detection precedence, and the error-text skeleton.

import test from "node:test";
import assert from "node:assert/strict";

import MESSAGES from "../../static/i18n.messages.js";

globalThis.HCRM_MESSAGES = MESSAGES;
// Dynamic import AFTER the global is set: the production order is messages → runtime,
// and this test file states that dependency instead of relying on hoisting.
const { default: HcrmI18n } = await import("../../static/i18n.js");

const {
  translate, plural, detectLang, applyLang, dateLocale, errorText,
  money, moneyCode, hasMessage, messagesFor, missingCount,
  LANG_KEY, SUPPORTED_LANGS, DEFAULT_LANG, FALLBACK_LANG, MONEY_CODE,
} = HcrmI18n;

/** Run `fn` with a temporary message table; always restores the real bundle. */
function withMessages(tables, fn) {
  const real = globalThis.HCRM_MESSAGES;
  globalThis.HCRM_MESSAGES = tables;
  try {
    return fn();
  } finally {
    globalThis.HCRM_MESSAGES = real;
  }
}

/** Run `fn` with console.error captured; returns the captured lines. */
function captureErrors(fn) {
  const real = console.error;
  const seen = [];
  console.error = (message) => seen.push(String(message));
  try {
    fn();
  } finally {
    console.error = real;
  }
  return seen;
}

test("translate: both languages resolve from the compiled copy", () => {
  assert.equal(translate("app.title", null, "en"), "HCRM — Shop Catalogue");
  assert.equal(translate("app.title", null, "da"), "HCRM — Butikskatalog");
  assert.equal(translate("catalogue.search_placeholder", null, "da"), "Søg i kataloget…");
  assert.equal(DEFAULT_LANG, "en");
  assert.equal(FALLBACK_LANG, "en");
  assert.deepEqual(SUPPORTED_LANGS.slice().sort(), ["da", "en"]);
  assert.equal(LANG_KEY, "hcrm_lang");
});

test("translate: an unknown language falls back to English, without logging", () => {
  const errors = captureErrors(() => {
    assert.equal(translate("app.title", null, "fr"), "HCRM — Shop Catalogue");
    assert.equal(translate("app.title"), "HCRM — Shop Catalogue", "no lang means the default");
  });
  assert.deepEqual(errors, []);
});

test("translate: a row missing in `da` falls back to English silently", () => {
  const errors = captureErrors(() => {
    const text = withMessages({ en: { "demo.only": "English only" }, da: {} },
      () => translate("demo.only", null, "da"));
    assert.equal(text, "English only");
  });
  assert.deepEqual(errors, []);
});

test("translate: a missing id renders the marker and logs once per id", () => {
  const before = missingCount();
  const errors = captureErrors(() => {
    assert.equal(translate("nope.nothing", null, "en"), "[missing: nope.nothing]");
    assert.equal(translate("nope.nothing", null, "da"), "[missing: nope.nothing]");
  });
  assert.equal(errors.length, 1, `one console.error per id, got ${JSON.stringify(errors)}`);
  assert.match(errors[0], /^\[i18n\] missing message: nope\.nothing/);
  assert.ok(errors[0].includes("i18n/strings.csv"), "the log names the file to fix");
  assert.equal(missingCount() - before, 2, "the counter counts lookups, the log dedupes");
});

test("translate/plural: a missing fallback row names the selected missing id", () => {
  const errors = captureErrors(() => {
    withMessages({ en: {}, da: {} }, () => {
      assert.equal(translate("ghost.row", null, "fr"), "[missing: ghost.row]",
        "an unsupported language cannot hide a missing English fallback");
      assert.equal(plural("ghost", 1, null, "da"), "[missing: ghost.one]",
        "plural failure names the missing .one row, not its base");
    });
  });
  assert.equal(errors.length, 2);
  assert.match(errors[0], /ghost\.row/);
  assert.match(errors[1], /ghost\.one/);
});

test("translate: interpolation is single-pass, unknown tokens stay, `$` is inert", () => {
  assert.equal(translate("common.greeting", { name: "Ada" }, "en"), "Hello, Ada!");
  assert.equal(translate("common.greeting", { name: "Ada" }, "da"), "Hej, Ada!");
  assert.equal(translate("common.greeting", null, "en"), "Hello, {name}!");
  assert.equal(translate("common.greeting", { name: "A {b}" }, "en"), "Hello, A {b}!");
  assert.equal(translate("common.greeting", { name: "$& $1 $`" }, "en"), "Hello, $& $1 $`!",
    "a value is never interpreted as a replacement pattern");
});

test("plural: .one only for exactly 1, {n} injected, explicit vars.n wins", () => {
  assert.equal(plural("common.item_count", 1, null, "en"), "1 item");
  assert.equal(plural("common.item_count", 0, null, "en"), "0 items");
  assert.equal(plural("common.item_count", 2, null, "en"), "2 items");
  assert.equal(plural("common.item_count", 1, null, "da"), "1 vare");
  assert.equal(plural("common.item_count", 0, null, "da"), "0 varer");
  assert.equal(plural("common.item_count", -3, null, "en"), "-3 items");
  assert.equal(plural("common.item_count", NaN, null, "en"), "0 items",
    "a non-finite count renders as 0, never NaN");
  assert.equal(plural("common.item_count", 5, { n: 9 }, "en"), "9 items",
    "an explicit vars.n wins");
  assert.equal(plural("common.item_count", 1, { extra: "x" }, "en"), "1 item");
});

test("detectLang: ?lang beats storage beats the default; junk is ignored", () => {
  const store = (value) => ({ getItem: (key) => (key === LANG_KEY ? value : null) });
  assert.equal(detectLang(null, "?lang=da"), "da");
  assert.equal(detectLang(store("en"), "?lang=da"), "da", "the query wins");
  assert.equal(detectLang(store("da"), "?lang=xx"), "da", "an invalid query is ignored");
  assert.equal(detectLang(store("da"), ""), "da");
  assert.equal(detectLang(store("fr"), ""), "en", "an invalid stored value is ignored");
  assert.equal(detectLang(null, ""), "en", "a fresh profile is English in item 2");
  assert.equal(detectLang({ getItem() { throw new Error("denied"); } }, ""), "en",
    "storage that throws (private mode) falls back");
  let writes = 0;
  detectLang({ getItem: () => null, setItem: () => { writes += 1; } }, "?lang=da");
  assert.equal(writes, 0, "detectLang never writes a preference");
});

test("applyLang: sets <html lang> and the title from the copy, idempotently", () => {
  const doc = { documentElement: {}, title: "" };
  assert.equal(applyLang("da", doc), "da");
  assert.equal(doc.documentElement.lang, "da");
  assert.equal(doc.title, "HCRM — Butikskatalog");
  assert.equal(applyLang("en", doc), "en");
  assert.equal(doc.documentElement.lang, "en");
  assert.equal(doc.title, "HCRM — Shop Catalogue");
  assert.equal(applyLang("de", doc), "en", "an unsupported language normalises to the default");
  assert.equal(doc.documentElement.lang, "en");
  assert.equal(applyLang("en", null), "en", "a missing doc must not throw");
});

test("moneyCode: the compiled row wins, the item-1 literal is the last resort", () => {
  assert.equal(moneyCode(), "DKK");
  assert.equal(MONEY_CODE, "DKK");
  assert.equal(withMessages({ en: { "money.code": "SEK" }, da: {} }, moneyCode), "SEK");
  assert.equal(withMessages({ en: {}, da: {} }, moneyCode), "DKK");
  assert.equal(withMessages({ en: { "money.code": "   " }, da: {} }, moneyCode), "DKK");
});

test("dateLocale: one explicit BCP 47 tag per UI language", () => {
  assert.equal(dateLocale("da"), "da-DK");
  assert.equal(dateLocale("en"), "en-GB");
  assert.equal(dateLocale("fr"), "en-GB");
});

test("errorText: code row, status row, validation row, then the raw message", () => {
  const errors = captureErrors(() => {
    // No error.* rows exist today: every branch falls through to the raw message.
    assert.equal(errorText({ message: "Not authenticated", status: 401 }, "en"), "Not authenticated");
    assert.equal(errorText({ detail: { status: "badge", message: "Scan a badge" } }, "en"), "Scan a badge");
    assert.equal(errorText({ detail: "plain detail" }, "en"), "plain detail");
    assert.equal(errorText("a bare string", "en"), "a bare string");
    assert.equal(errorText({ status: 422, detail: [{ msg: "bad" }] }, "en"), "[missing: error.request_failed]",
      "no message at all is loud, not blank");

    withMessages({
      en: {
        "error.badge": "Badge problem: {detail}",
        "error.status.misread": "That scan was a misread",
        "error.validation": "Check the highlighted field",
      },
      da: {},
    }, () => {
      assert.equal(errorText({ code: "badge", vars: { detail: "expired" } }, "en"),
        "Badge problem: expired", "a machine code wins");
      assert.equal(errorText({ detail: { code: "badge", vars: { detail: "x" } } }, "en"),
        "Badge problem: x", "the structured detail code is equivalent");
      assert.equal(errorText({ detail: { status: "misread" } }, "en"), "That scan was a misread");
      assert.equal(errorText({ status: 422, detail: [{ msg: "bad" }] }, "en"),
        "Check the highlighted field");
    });
  });
  assert.equal(errors.length, 1, "only the no-message case logs (it is a real gap)");
});

test("hasMessage/messagesFor are quiet probes over the compiled table", () => {
  const errors = captureErrors(() => {
    assert.equal(hasMessage("app.title", "en"), true);
    assert.equal(hasMessage("app.title", "da"), true);
    assert.equal(hasMessage("nope.nothing", "en"), false);
    assert.equal(hasMessage("nope.nothing", "da"), false);
    assert.equal(messagesFor("en")["money.code"], "DKK");
    assert.equal(messagesFor("fr")["app.title"], "HCRM — Shop Catalogue", "unknown lang → fallback");
  });
  assert.deepEqual(errors, []);
});

test("money: item 1's contract is unchanged by the runtime growth", () => {
  assert.equal(money(0), "0,00 DKK");
  assert.equal(money(5), "0,05 DKK");
  assert.equal(money(-5), "-0,05 DKK");
  assert.equal(money(999), "9,99 DKK");
  assert.equal(money(123450), "1.234,50 DKK");
  assert.equal(money(123456789), "1.234.567,89 DKK");
  assert.equal(money(-1234.5), "-12,35 DKK");
  assert.equal(money(undefined), "0,00 DKK");
  assert.equal(money(NaN), "0,00 DKK");
  assert.equal(money(123450, "kr."), "1.234,50 kr.", "the explicit argument still wins");
  assert.equal(money(123450, moneyCode()), "1.234,50 DKK", "the compiled code is the default");
});
