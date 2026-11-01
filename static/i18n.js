/* HCRM i18n runtime — pure, dependency-free, no DOM, no locale services.
 *
 * Item 1 of the Danish/DKK roadmap seeds this file with the money
 * formatter only. Item 2 grows it into the language runtime (message
 * lookup, plurals, language detection) and the currency token then comes
 * from the copy CSV's money.code row instead of the MONEY_CODE constant
 * below (the second argument is that injection hook).
 *
 * The formatter is hand-rolled integer arithmetic on purpose: the
 * platform's currency formatting emits a NON-BREAKING SPACE before the
 * code, moves the code to the far side in other locales, and its exact
 * bytes drift with the browser's ICU build — while this repo's harnesses
 * pin rendered strings byte-exactly. Fixed Danish numerals in BOTH UI
 * languages: money must never change shape when the language toggles
 * ("1.234,50" vs "1,234.50" is a thousands/decimal misread hazard).
 *
 * Dual classic-script/CommonJS export, exactly the scan-capture /
 * scan-feed idiom, so the node suite imports the same object the browser
 * gets as a plain global.
 */
(function (global) {
  // The compile-time default. Item 2 makes the CSV row `money.code` the source
  // of truth and passes it in as the second argument.
  const MONEY_CODE = "DKK";

  function money(cents, code) {
    let n = Number(cents);
    if (!Number.isFinite(n)) n = 0;                 // undefined/null/NaN/±Infinity/"x"/{} -> 0
    let ore = Math.sign(n) * Math.round(Math.abs(n)); // whole øre, half AWAY from zero
    if (!Number.isSafeInteger(ore)) ore = Math.sign(ore) * Number.MAX_SAFE_INTEGER; // plain digits, sign kept
    const sign = ore < 0 ? "-" : "";                 // -0.4 and -0 render "0,00", never "-0,00"
    const abs = Math.abs(ore);
    const kroner = String(Math.floor(abs / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    const oere = String(abs % 100).padStart(2, "0");
    return sign + kroner + "," + oere + " " + (code || MONEY_CODE);
  }

  const HcrmI18n = { MONEY_CODE, money };
  if (typeof module !== "undefined" && module.exports) module.exports = HcrmI18n;
  global.HcrmI18n = HcrmI18n;
})(typeof window !== "undefined" ? window : globalThis);
