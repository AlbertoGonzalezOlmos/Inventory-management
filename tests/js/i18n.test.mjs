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
  money, moneyCode, hasMessage, messagesFor, missingCount, fieldLabel,
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
  assert.equal(translate("app.title", null, "en"), "Hertha's Butik — Shop Catalogue");
  assert.equal(translate("app.title", null, "da"), "Hertha's Butik — Butikskatalog");
  assert.equal(translate("app.brand", null, "en"), "Hertha's Butik");
  assert.equal(translate("app.brand", null, "da"), "Hertha's Butik",
    "the brand is a proper noun: identical in both languages");
  assert.equal(translate("catalogue.search_placeholder", null, "da"), "Søg i kataloget…");
  // Item 7's flip: the default UI language is Danish. The copy assertions above
  // stay CSV pins; anything about the DEFAULT derives from the constant.
  assert.equal(DEFAULT_LANG, "da");
  assert.equal(FALLBACK_LANG, "en");
  assert.deepEqual(SUPPORTED_LANGS.slice().sort(), ["da", "en"]);
  assert.equal(LANG_KEY, "hcrm_lang");
});

test("translate: an unknown language normalises to the DEFAULT, without logging", () => {
  const errors = captureErrors(() => {
    assert.equal(translate("app.title", null, "fr"), MESSAGES[DEFAULT_LANG]["app.title"]);
    assert.equal(translate("app.title"), MESSAGES[DEFAULT_LANG]["app.title"],
      "no lang means the default");
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
  assert.equal(translate("auth.greeting", { name: "Ada" }, "en"), "Hi Ada");
  assert.equal(translate("auth.greeting", { name: "Ada" }, "da"), "Hej Ada");
  assert.equal(translate("auth.greeting", null, "en"), "Hi {name}");
  assert.equal(translate("auth.greeting", { name: "A {b}" }, "en"), "Hi A {b}");
  assert.equal(translate("auth.greeting", { name: "$& $1 $`" }, "en"), "Hi $& $1 $`",
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
  assert.equal(detectLang(store("fr"), ""), DEFAULT_LANG,
    "an invalid stored value is ignored");
  assert.equal(detectLang(null, ""), DEFAULT_LANG,
    "a fresh profile gets the DEFAULT language (Danish since the flip)");
  assert.equal(detectLang({ getItem() { throw new Error("denied"); } }, ""), DEFAULT_LANG,
    "storage that throws (private mode) falls back");
  let writes = 0;
  detectLang({ getItem: () => null, setItem: () => { writes += 1; } }, "?lang=da");
  assert.equal(writes, 0, "detectLang never writes a preference");
});

test("applyLang: sets <html lang> and the title from the copy, idempotently", () => {
  const doc = { documentElement: {}, title: "" };
  assert.equal(applyLang("da", doc), "da");
  assert.equal(doc.documentElement.lang, "da");
  assert.equal(doc.title, "Hertha's Butik — Butikskatalog");
  assert.equal(applyLang("en", doc), "en");
  assert.equal(doc.documentElement.lang, "en");
  assert.equal(doc.title, "Hertha's Butik — Shop Catalogue");
  assert.equal(applyLang("de", doc), DEFAULT_LANG,
    "an unsupported language normalises to the default");
  assert.equal(doc.documentElement.lang, DEFAULT_LANG);
  assert.equal(doc.title, MESSAGES[DEFAULT_LANG]["app.title"]);
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
  assert.equal(dateLocale("fr"), dateLocale(DEFAULT_LANG),
    "an unsupported language follows the default's locale");
});

test("errorText: item 6's chain, executed against the shipped copy", () => {
  const errors = captureErrors(() => {
    // 1. the response header's code -> error.<code> (the row IS the copy: an
    //    English server message never wins when the code has a row).
    assert.equal(errorText({ code: "not_authenticated", status: 401,
                             message: "Not authenticated" }, "en"),
      MESSAGES.en["error.not_authenticated"]);
    assert.equal(errorText({ code: "not_authenticated", status: 401 }, "da"),
      MESSAGES.da["error.not_authenticated"]);
    // ...with the parameterised values from X-Error-Vars (one var, two spans)
    assert.equal(errorText({ code: "barcode_taken", vars: { sku: "SKU-A" },
                             message: "Barcode already assigned to item SKU-A" }, "da"),
      MESSAGES.da["error.barcode_taken"].replace("{sku}", "SKU-A"));
    assert.equal(errorText({ code: "terminal_not_found",
                             vars: { terminal: "'kiosk-1'" } }, "en"),
      MESSAGES.en["error.terminal_not_found"].replace("{terminal}", "'kiosk-1'"));
    // A row whose vars did not travel is abandoned for the raw message: a
    // visible "{sku}" is worse than the server's (English) sentence.
    assert.equal(errorText({ code: "barcode_taken",
                             message: "Barcode already assigned to item SKU-A" }, "da"),
      "Barcode already assigned to item SKU-A");

    // 2. no header (a proxy stripped it): the structured status family. No
    //    error.status.* rows ship — the code is authoritative — so this is
    //    proved with a temporary table, and the raw message is the live path.
    assert.equal(errorText({ detail: { status: "badge", message: "Scan a badge" } }, "en"),
      "Scan a badge");
    withMessages({
      en: { "error.status.misread": "That scan was a misread" },
      da: { "error.status.misread": "Det var en fejllæsning" },
    }, () => {
      assert.equal(errorText({ detail: { status: "misread" } }, "en"),
        "That scan was a misread");
      assert.equal(errorText({ detail: { status: "misread" } }, "da"),
        "Det var en fejllæsning");
    });

    // 3. FastAPI's own 422: the field label row, from the body's loc.
    assert.equal(errorText({ status: 422, field: "price_cents" }, "da"),
      MESSAGES.da["error.validation_field"].replace("{field}", MESSAGES.da["common.price"]));
    assert.equal(errorText({ status: 422 }, "en"), MESSAGES.en["error.validation"]);
    // An unmapped field name is a machine identifier: it renders verbatim.
    assert.equal(errorText({ status: 422, field: "symbology" }, "en"),
      MESSAGES.en["error.validation_field"].replace("{field}", "symbology"));

    // 4. uncoded: the raw text (a browser string, or a message no row covers)
    assert.equal(errorText({ status: 500, message: "Internal server error" }, "en"),
      "Internal server error");
    assert.equal(errorText("a bare string", "en"), "a bare string");
    // 5. nothing at all: the request_failed row — never blank
    assert.equal(errorText({ status: 500 }, "da"),
      MESSAGES.da["error.request_failed"].replace("{status}", "500"));
    assert.equal(errorText(null, "en"),
      MESSAGES.en["error.request_failed"].replace("{status}", "?"));
  });
  assert.deepEqual(errors, [], "the chain is quiet: it probes, it never logs");
});

test("fieldLabel maps the 422 field names to their rows", () => {
  assert.equal(fieldLabel("price_cents", "da"), MESSAGES.da["common.price"]);
  assert.equal(fieldLabel("new_password", "en"), MESSAGES.en["field.new_password"]);
  assert.equal(fieldLabel("barcode", "da"), MESSAGES.da["field.barcode"]);
  assert.equal(fieldLabel("delta", "en"), MESSAGES.en["field.delta"]);
  assert.equal(fieldLabel("image_url", "en"), MESSAGES.en["field.image_url"]);
  assert.equal(fieldLabel("unmapped_field", "da"), "unmapped_field");
  assert.equal(fieldLabel(null, "en"), "");
});

test("hasMessage/messagesFor are quiet probes over the compiled table", () => {
  const errors = captureErrors(() => {
    assert.equal(hasMessage("app.title", "en"), true);
    assert.equal(hasMessage("app.title", "da"), true);
    assert.equal(hasMessage("nope.nothing", "en"), false);
    assert.equal(hasMessage("nope.nothing", "da"), false);
    assert.equal(messagesFor("en")["money.code"], "DKK");
    assert.equal(messagesFor("fr")["app.title"], MESSAGES[DEFAULT_LANG]["app.title"],
      "unknown lang → the DEFAULT table");
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
