/* HCRM i18n runtime — pure, dependency-free message lookup + the money formatter.
 *
 * Two layers live here, and they are deliberately different in kind:
 *
 * 1. `money()` — the DKK formatter from item 1. Byte-identical to the version the
 *    item-1 pins froze: hand-rolled integer arithmetic, Danish numerals in every UI
 *    language, ASCII space, no Intl (which emits an NBSP before the code and drifts
 *    with the browser's ICU build).
 * 2. The message runtime from item 2 — lookup, plurals, language detection,
 *    `<html lang>`/title application, the date locale and (since item 6) the
 *    server-error chain: an `X-Error-Code` header → its `error.<code>` row, with
 *    the header's vars, falling back to the structured `status` family, FastAPI's
 *    422 (field label), the raw message and finally `error.request_failed`.
 *
 * The copy itself is NOT in this file: `static/i18n.messages.js` (generated from
 * i18n/strings.csv by scripts/build_i18n.py) publishes `window.HCRM_MESSAGES` and is
 * loaded BEFORE this file. Node tests import the same two files (each `node --test`
 * file runs in its own process, so importing only this one keeps the "DKK" fallback
 * honest for the money suite).
 *
 * Defaults: `DEFAULT_LANG = "en"` (item 7 flips that single constant), no persistence
 * writes, no query rewriting — this item is machinery only and renders nothing new.
 *
 * Dual classic-script/CommonJS export, exactly the scan-capture / scan-feed idiom.
 */
(function (global) {
  // The compile-time default for the currency token; the CSV row `money.code`
  // overrides it through moneyCode() once the bundle is loaded.
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

  /* ---------- message runtime (item 2) ---------- */

  const LANG_KEY = "hcrm_lang";              // the hcrm_* storage family
  const SUPPORTED_LANGS = ["da", "en"];
  const DEFAULT_LANG = "en";                 // item 7: the flip commit changes this one line
  const FALLBACK_LANG = "en";                // the chain is lang -> en -> marker, always
  const MISSING_PREFIX = "[missing: ";

  let missingCountValue = 0;
  const reportedMissing = Object.create(null);   // one console.error per id, per page load

  /** The generated bundle, read live so a later load (tests) is honoured. */
  function messages() {
    return global.HCRM_MESSAGES || {};
  }

  function normaliseLang(lang) {
    return SUPPORTED_LANGS.indexOf(lang) >= 0 ? lang : DEFAULT_LANG;
  }

  function messagesFor(lang) {
    const all = messages();
    return all[normaliseLang(lang)] || all[FALLBACK_LANG] || {};
  }

  /** True when `id` has a row in `lang` or in the fallback language. Quiet. */
  function hasMessage(id, lang) {
    if (typeof messagesFor(lang)[id] === "string") return true;
    const fallback = messages()[FALLBACK_LANG] || {};
    return typeof fallback[id] === "string";
  }

  /** The currency token: the compiled CSV row first, the item-1 literal last. */
  function moneyCode() {
    const en = messages()[FALLBACK_LANG] || {};
    const code = en["money.code"];
    return typeof code === "string" && code.trim() ? code.trim() : MONEY_CODE;
  }

  /** Single pass: a value that itself contains {x} is inserted verbatim. */
  function interpolate(text, vars) {
    if (!vars) return text;
    return text.replace(/\{([a-z][a-z0-9_]*)\}/g, (whole, name) =>
      Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole);
  }

  /**
   * translate(id, vars?, lang?) -> string. Resolution: lang -> fallback -> marker.
   * Never throws and never returns undefined (a throw inside a Vue render would blank
   * the SPA). A missing id is loud: the `[missing: id]` marker renders where the copy
   * would have been, and one console.error per id names the file to fix — that message
   * is in scripts/ui_check.py's FAIL_PATTERNS, so the browser gate fails with it.
   */
  function translate(id, vars, lang) {
    const resolved = normaliseLang(lang);
    let text = messagesFor(resolved)[id];
    if (typeof text !== "string") {
      const fallback = messages()[FALLBACK_LANG] || {};
      text = fallback[id];
      if (typeof text !== "string") {
        missingCountValue += 1;
        if (!reportedMissing[id]) {
          reportedMissing[id] = true;
          console.error("[i18n] missing message: " + id +
            " — add it to i18n/strings.csv and run: uv run python scripts/build_i18n.py --write");
        }
        return MISSING_PREFIX + id + "]";
      }
    }
    return interpolate(text, vars);
  }

  /**
   * plural(base, n, vars?, lang?) -> string. `.one` iff the count is exactly 1 (Danish
   * uses `.other` for 0, which is what this gives). `{n}` is supplied automatically;
   * an explicit `vars.n` wins. Non-finite input renders as 0 rather than "NaN".
   */
  function plural(base, n, vars, lang) {
    const count = Number(n);
    const value = Number.isFinite(count) ? count : 0;
    const merged = Object.assign({ n: value }, vars || {});
    return translate(base + (value === 1 ? ".one" : ".other"), merged, lang);
  }

  /** The language a switch would land on (normalising junk to the en side). */
  function otherLang(lang) {
    return normaliseLang(lang) === "da" ? "en" : "da";
  }

  function hasLangParam(search) {
    return typeof search === "string" && /[?&]lang=/.test(search);
  }

  /** Remove ONLY the `lang` pair, preserving order, sibling params and the
   *  caller's hash: "?a=1&lang=da&b=2" -> "?a=1&b=2"; "?lang=da" -> "";
   *  "?language=da" is untouched (a different parameter). Pure. */
  function stripLangQuery(search) {
    if (!hasLangParam(search)) return typeof search === "string" ? search : "";
    const kept = search.replace(/^\?/, "").split("&")
      .filter((pair) => pair && !/^lang=/.test(pair));
    return kept.length ? "?" + kept.join("&") : "";
  }

  /** The one-shot boot decision (item 3): resolve `?lang` > stored > default,
   *  and say whether the query should be persisted and stripped. `lang` is
   *  resolved HERE and never re-derived — after the strip, a storage-blocked
   *  device must not silently revert a consumed ?lang. Pure: no writes. */
  function bootPlan(search, storage) {
    return {
      lang: detectLang(storage, search),
      persist: queryLang(search) !== null,
      strip: hasLangParam(search),
      search: stripLangQuery(search),
    };
  }

  /** Transient-message renderer: `{key, vars}` descriptors go through the
   *  passed translate (so they re-render with the language), anything else
   *  passes through verbatim, null/undefined render "". Never throws. */
  function descriptorText(value, translateFn) {
    if (value == null) return "";
    if (typeof value === "object" && typeof value.key === "string") {
      return String(translateFn(value.key, value.vars || null));
    }
    return String(value);
  }

  function queryLang(search) {
    if (typeof search !== "string" || !search) return null;
    const match = /[?&]lang=([^&]*)/.exec(search);
    if (!match) return null;
    let value = match[1];
    try {
      value = decodeURIComponent(value);
    } catch (_) {
      return null;
    }
    return SUPPORTED_LANGS.indexOf(value) >= 0 ? value : null;
  }

  /**
   * detectLang(storage?, search?) -> "da"|"en". Precedence: `?lang=` -> stored
   * preference -> DEFAULT_LANG. Reading storage is guarded: Safari's private mode and
   * a locked-down kiosk both throw on access. This function NEVER writes.
   */
  function detectLang(storage, search) {
    const fromQuery = queryLang(search);
    if (fromQuery) return fromQuery;
    try {
      const store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
      const stored = store && store.getItem(LANG_KEY);
      if (SUPPORTED_LANGS.indexOf(stored) >= 0) return stored;
    } catch (_) {
      /* storage unavailable: fall through to the default */
    }
    return DEFAULT_LANG;
  }

  /**
   * applyLang(lang, doc?) -> lang. Sets `<html lang>` and the document title from the
   * compiled copy. `doc` is a parameter so this module never reaches for the DOM on its
   * own (and so node tests pass a fake). Idempotent.
   */
  function applyLang(lang, doc) {
    const resolved = normaliseLang(lang);
    const target = doc || (typeof document !== "undefined" ? document : null);
    if (target && target.documentElement) {
      target.documentElement.lang = resolved;
      target.title = translate("app.title", null, resolved);
    }
    return resolved;
  }

  /** The BCP 47 tag for NUMERIC dates (month/weekday names are CSV rows, item 5). */
  function dateLocale(lang) {
    return normaliseLang(lang) === "da" ? "da-DK" : "en-GB";
  }

  /** The visible label for a pydantic field name — a 422's ``loc[-1]``.
   *  A map, not a computed id: the wire field name is a MACHINE value and
   *  these rows are the only labels a validation sentence needs. An unmapped
   *  name renders verbatim (it is a machine identifier, never blank). */
  const FIELD_LABELS = {
    name: "field.name", sku: "field.sku", category: "field.category",
    description: "field.description", stock: "field.stock", role: "field.role",
    status: "field.status", barcode: "field.barcode", delta: "field.delta",
    image_url: "field.image_url", new_password: "field.new_password",
    price_cents: "common.price", qty: "common.qty", item_id: "common.item",
    code: "table.col.code", email: "auth.email", password: "auth.password",
    current_password: "auth.current_password", new_email: "account.new_email",
    phone: "account.phone",
  };

  function fieldLabel(name, lang) {
    const id = FIELD_LABELS[name];
    return id && hasMessage(id, lang) ? translate(id, null, lang) : String(name == null ? "" : name);
  }

  /** A leftover {token}: the row was used with a var the caller did not have. */
  const TOKEN_LEFT = /\{[a-z][a-z0-9_]*\}/;

  /**
   * errorText(err, lang?) -> string. The server's error, in the UI language.
   *
   * Chain (item 6):
   *   1. the response's `X-Error-Code` header -> the `error.<code>` row, with
   *      the `X-Error-Vars` values (so a parameterised sentence renders whole);
   *   2. a structured detail's `status` discriminator -> `error.status.<status>`
   *      (the header stripped by a proxy, or an older server);
   *   3. FastAPI's own 422 -> `error.validation_field` with the field's label,
   *      else `error.validation`;
   *   4. nothing matched: the raw message (an UNCODED error — a browser string
   *      or a server text no row covers), and finally
   *   5. `error.request_failed` — never blank.
   * A row whose placeholders the caller cannot fill is abandoned for step 4
   * rather than shipped with a visible `{token}`; the server always publishes
   * the vars for a parameterised code, so this is a safety net, not a path.
   * Quiet: probes go through hasMessage, never translate, so an error path
   * cannot emit missing-key noise.
   */
  function errorText(err, lang) {
    if (err == null) return translate("error.request_failed", { status: "?" }, lang);
    const detail = err.detail;
    const detailObj = detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null;
    const vars = (err.vars) || (detailObj && detailObj.vars) || null;

    // 1. the coded response
    const code = (typeof err.code === "string" && err.code) ||
                 (detailObj && typeof detailObj.code === "string" && detailObj.code) || null;
    if (code && hasMessage("error." + code, lang)) {
      const values = Object.assign({}, vars);
      if (code === "validation_field") values.field = fieldLabel(err.field, lang);
      const text = translate("error." + code, values, lang);
      if (!TOKEN_LEFT.test(text)) return text;
    }

    // 2. a structured envelope's status discriminator
    const status = detailObj && detailObj.status;
    if (typeof status === "string" && hasMessage("error.status." + status, lang)) {
      return translate("error.status." + status, vars, lang);
    }

    // 3. FastAPI's own 422 (no header: the field label is the best copy)
    if (err.status === 422) {
      if (err.field && hasMessage("error.validation_field", lang)) {
        return translate("error.validation_field",
                         { field: fieldLabel(err.field, lang) }, lang);
      }
      if (hasMessage("error.validation", lang)) {
        return translate("error.validation", null, lang);
      }
    }

    // 4. uncoded: the raw text (a browser string, or a server message with no row)
    const raw = (err.message) ||
      (detailObj && detailObj.message) ||
      (typeof detail === "string" ? detail : "") ||
      (typeof err === "string" ? err : "");
    if (raw) return String(raw);

    // 5. never blank
    return translate("error.request_failed",
                     { status: err.status || "?" }, lang);
  }

  /** How many lookups returned the missing marker (item 3 binds this reactively). */
  function missingCount() {
    return missingCountValue;
  }

  const HcrmI18n = {
    MONEY_CODE, LANG_KEY, SUPPORTED_LANGS, DEFAULT_LANG, FALLBACK_LANG,
    money, moneyCode, messagesFor, hasMessage, translate, plural,
    detectLang, applyLang, dateLocale, errorText, missingCount, fieldLabel,
    otherLang, queryLang, stripLangQuery, bootPlan, descriptorText,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = HcrmI18n;
  global.HcrmI18n = HcrmI18n;
})(typeof window !== "undefined" ? window : globalThis);
