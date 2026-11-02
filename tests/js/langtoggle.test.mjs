// Language-switch tests (item 3) — `node --test tests/js/`.
//
// Real files only: the compiled bundle (loaded first, exactly as
// static/index.html orders the tags), the runtime, the REAL static/app.js
// methods (brace-extracted, the loadmore.test.mjs idiom), and — where
// reactivity itself is the claim — the REAL vendored Vue (it loads under
// node: an open toast must re-render through t()/toastText when `lang`
// flips, which no mock can prove).

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
const vueSrc = readFileSync(join(ROOT, "static", "vendor", "vue.global.prod.js"), "utf8");
const Vue = new Function(vueSrc + "; return Vue;")();

const {
  otherLang, queryLang, stripLangQuery, bootPlan, descriptorText,
  detectLang, applyLang, translate,
} = HcrmI18n;

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

function appObject(...headers) {
  const body = headers.map(extractMethod).join(",\n");
  return new Function("HcrmI18n", `"use strict"; return ({\n${body}\n});`)(HcrmI18n);
}

const store = (value) => ({
  writes: [],
  getItem(key) { return key === "hcrm_lang" ? value : null; },
  setItem(key, v) { this.writes.push([key, v]); },
});
const throwingStore = { getItem() { throw new Error("denied"); }, setItem() {} };

// --- pure runtime helpers ---------------------------------------------------

test("otherLang round-trips and normalises junk to the en side", () => {
  assert.equal(otherLang("en"), "da");
  assert.equal(otherLang("da"), "en");
  assert.equal(otherLang(otherLang("en")), "en");
  assert.equal(otherLang("fr"), "da", "junk normalises to the default first");
  assert.equal(otherLang(undefined), "da");
});

test("stripLangQuery removes only the lang pair", () => {
  assert.equal(stripLangQuery("?a=1&lang=da&b=2"), "?a=1&b=2");
  assert.equal(stripLangQuery("?lang=da"), "");
  assert.equal(stripLangQuery("?language=da"), "?language=da", "a different parameter");
  assert.equal(stripLangQuery(""), "");
  assert.equal(stripLangQuery(undefined), "");
  assert.equal(stripLangQuery("?x=1"), "?x=1");
});

test("bootPlan: ?lang beats stored, stored beats default, junk is stripped not persisted", () => {
  assert.deepEqual(bootPlan("", store(null)),
    { lang: "en", persist: false, strip: false, search: "" });
  assert.deepEqual(bootPlan("", store("da")),
    { lang: "da", persist: false, strip: false, search: "" });
  assert.deepEqual(bootPlan("?lang=da", store("en")),
    { lang: "da", persist: true, strip: true, search: "" });
  assert.deepEqual(bootPlan("?x=1&lang=da", store(null)),
    { lang: "da", persist: true, strip: true, search: "?x=1" });
  assert.deepEqual(bootPlan("?lang=fr", store(null)),
    { lang: "en", persist: false, strip: true, search: "" },
    "junk is cleaned from the URL but never persisted");
  const plan = bootPlan("?lang=da", throwingStore);
  assert.equal(plan.lang, "da", "a storage-blocked device still applies ?lang");
  assert.equal(plan.persist, true);
  assert.equal(queryLang("?lang=da"), "da");
  assert.equal(queryLang("?lang=fr"), null);
});

test("descriptorText: strings pass, {key,vars} render, null is empty, never throws", () => {
  const t = (id, vars) => translate(id, vars, "en");
  assert.equal(descriptorText("plain", t), "plain");
  assert.equal(descriptorText({ key: "auth.greeting", vars: { name: "Ada" } }, t),
    "Hi Ada");
  assert.equal(descriptorText({ key: "nope.row" }, t), "[missing: nope.row]");
  assert.equal(descriptorText(null, t), "");
  assert.equal(descriptorText(undefined, t), "");
});

test("a plural toast descriptor picks .one/.other and re-renders on a flip", () => {
  // {key, plural, vars?} is the slice-B extension of the descriptor contract:
  // the count picks the sibling row through tp(), and msg() still reads
  // this.lang at paint time, so an open count toast flips with the flag (a
  // {key, vars} descriptor has no way to choose .one vs .other).
  const m = appObject("t(id, vars) {", "tp(base, n, vars) {", "msg(value) {",
                      "_counts(vars) {");
  const state = { lang: "en", ...m };
  assert.equal(state.msg({ key: "cart.restored", plural: 1 }),
    MESSAGES.en["cart.restored.one"].replace("{n}", "1"));
  assert.equal(state.msg({ key: "cart.restored", plural: 2 }),
    MESSAGES.en["cart.restored.other"].replace("{n}", "2"));
  state.lang = "da";
  assert.equal(state.msg({ key: "cart.imported", plural: 3 }),
    MESSAGES.da["cart.imported.other"].replace("{n}", "3"));
  assert.equal(state.msg({ key: "checkout.skipped", plural: 1 }),
    MESSAGES.da["checkout.skipped.one"].replace("{n}", "1"));
});


test("a TWO-count toast descriptor pluralises each count and re-renders on a flip", () => {
  // Slice E: the export toast says "Exported 3 rows × 2 columns as CSV" — ONE
  // plural pair per count cannot express that, and a phrase rendered at call
  // time would freeze the old language in an open toast. A var may therefore
  // be its own count descriptor, resolved at PAINT time (msg -> _counts -> tp),
  // so both counts follow the flag.
  const m = appObject("t(id, vars) {", "tp(base, n, vars) {", "msg(value) {",
                      "_counts(vars) {");
  const state = { lang: "en", ...m };
  const toast = {
    key: "export.done",
    vars: {
      rows: { key: "common.rows", plural: 1 },
      cols: { key: "common.cols", plural: 2 },
      format: "CSV",
    },
  };
  assert.equal(state.msg(toast), MESSAGES.en["export.done"]
    .replace("{rows}", MESSAGES.en["common.rows.one"].replace("{n}", "1"))
    .replace("{cols}", MESSAGES.en["common.cols.other"].replace("{n}", "2"))
    .replace("{format}", "CSV"));

  // Flip AND swap the counts: the plural choice moves with the number, and the
  // sentence with the language (3 rows -> .other, 1 column -> .one).
  state.lang = "da";
  assert.equal(state.msg({ ...toast, vars: {
    rows: { key: "common.rows", plural: 3 },
    cols: { key: "common.cols", plural: 1 },
    format: "CSV",
  } }), MESSAGES.da["export.done"]
    .replace("{rows}", MESSAGES.da["common.rows.other"].replace("{n}", "3"))
    .replace("{cols}", MESSAGES.da["common.cols.one"].replace("{n}", "1"))
    .replace("{format}", "CSV"));
});


test("applyLang sets <html lang> and the title from the bundle, both directions", () => {
  for (const lang of ["en", "da"]) {
    const doc = { documentElement: {}, title: "" };
    assert.equal(applyLang(lang, doc), lang);
    assert.equal(doc.documentElement.lang, lang);
    assert.equal(doc.title, MESSAGES[lang]["app.title"], "read from the bundle, not hardcoded");
  }
});

// --- the real app.js wiring ---------------------------------------------------

test("toggleLang flips and the watcher persists + applies, both directions", () => {
  // Both bodies are the SHIPPED ones: `toggleLang` flips through the
  // `nextLang` computed, so an inverted polarity in app.js fails here.
  const m = appObject("toggleLang() {", "nextLang() {");
  // The watcher's source references the app.js module constant LANG_KEY, so
  // it is compiled with that binding injected (the loadmore idiom, plus one
  // constant the method body expects in scope).
  const watcher = new Function(
    "HcrmI18n", "LANG_KEY",
    `"use strict"; return ({\n${extractMethod("lang(next) {")}\n}).lang;`
  )(HcrmI18n, "hcrm_lang");
  const writes = [];
  const doc = { documentElement: {}, title: "" };
  const realLocalStorage = globalThis.localStorage;
  const realDocument = globalThis.document;
  globalThis.localStorage = {
    getItem: () => null,
    setItem: (key, value) => writes.push([key, value]),
  };
  globalThis.document = doc;
  try {
    const vm = { lang: "en" };
    Object.assign(vm, m);
    // Computeds are unwrapped in the real app; this getter exposes the
    // shipped body's VALUE on the same reactive object.
    Object.defineProperty(vm, "nextLang", {
      get() { return m.nextLang.call(vm); },
    });
    vm.toggleLang();
    assert.equal(vm.lang, "da");
    watcher.call(vm, "da");
    assert.deepEqual(writes, [["hcrm_lang", "da"]], "the watcher is the writer");
    assert.equal(doc.documentElement.lang, "da");
    assert.equal(doc.title, MESSAGES.da["app.title"]);
    vm.toggleLang();
    assert.equal(vm.lang, "en");
    watcher.call(vm, "en");
    assert.deepEqual(writes, [["hcrm_lang", "da"], ["hcrm_lang", "en"]]);
    assert.equal(doc.documentElement.lang, "en");
    assert.equal(doc.title, MESSAGES.en["app.title"]);
  } finally {
    if (realLocalStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = realLocalStorage;
    if (realDocument === undefined) delete globalThis.document;
    else globalThis.document = realDocument;
  }
});

test("detectLang never writes (a first visit stays unwritten)", () => {
  const s = store(null);
  assert.equal(detectLang(s, ""), "en");
  assert.equal(detectLang(s, "?lang=da"), "da");
  assert.deepEqual(s.writes, []);
});

test("an open toast and greeting re-render through the real Vue when lang flips", async () => {
  // Every body here is the SHIPPED one, including the two computeds: the
  // test does not re-implement the descriptor path it is proving.
  const m = appObject("t(id, vars) {", "msg(value) {", "_counts(vars) {",
                      "nextLang() {", "toastText() {", "greetingText() {",
                      "toggleLang() {");
  const state = Vue.reactive({
    lang: "en",
    toast: { key: "auth.greeting", vars: { name: "Ada" } },
    greeting: { key: "auth.greeting", vars: { name: "Bo" } },
  });
  Object.assign(state, m);
  // Computeds are unwrapped in the real app (`this.toastText` is a value);
  // these getters expose the SHIPPED bodies' values on the reactive state,
  // so a body that stopped reading `lang` fails the render assertions.
  Object.defineProperty(state, "nextLang", {
    get() { return m.nextLang.call(state); },
  });
  Object.defineProperty(state, "toastText", {
    get() { return m.toastText.call(state); },
  });
  Object.defineProperty(state, "greetingText", {
    get() { return m.greetingText.call(state); },
  });

  const renders = [];
  Vue.watchEffect(() => {
    renders.push({
      label: state.t("lang.switch_to"),
      toast: state.toastText,
      greeting: state.greetingText,
    });
  });
  assert.equal(renders.length, 1);
  assert.equal(renders[0].label, MESSAGES.en["lang.switch_to"]);
  assert.equal(renders[0].toast, "Hi Ada");
  assert.equal(renders[0].greeting, "Hi Bo");

  state.toggleLang();
  await Vue.nextTick();

  assert.equal(renders.length, 2, "the flip re-rendered the open toast and greeting");
  assert.equal(renders[1].label, MESSAGES.da["lang.switch_to"]);
  assert.equal(renders[1].toast, MESSAGES.da["auth.greeting"].replace("{name}", "Ada"));
  assert.equal(renders[1].greeting, MESSAGES.da["auth.greeting"].replace("{name}", "Bo"));
  assert.equal(state.lang, "da");
});

test("plain-string transients neither change nor re-render (server messages)", async () => {
  const m = appObject("t(id, vars) {", "msg(value) {", "_counts(vars) {",
                      "toastText() {");
  // api() surfaces a server `detail` as a plain string (item 6 owns its copy),
  // so the plain-string path must keep passing it through untouched.
  const state = Vue.reactive({ lang: "en", toast: "Request failed (500)" });
  Object.assign(state, m);
  Object.defineProperty(state, "toastText", {
    get() { return m.toastText.call(state); },
  });
  const seen = [];
  Vue.watchEffect(() => seen.push(state.toastText));
  assert.deepEqual(seen, ["Request failed (500)"]);
  state.lang = "da";
  await Vue.nextTick();
  // A plain string never reads `lang` (descriptorText passes it through
  // without t()), so the effect has no dependency and does not even
  // re-run — a server message is untouched by a flip, by construction.
  assert.deepEqual(seen, ["Request failed (500)"]);
});

test("the SHIPPED boot block: applies, writes once, strips, keeps hash (and never on a first visit)", () => {
  // Replay the real top-level boot statements from static/app.js with fake
  // globals. The pure bootPlan matrix above predicts the plan; this proves
  // the SEQUENCE app.js actually runs: apply -> persist (guarded, once) ->
  // strip (guarded), before mount, with the hash and sibling params intact.
  const start = appSrc.indexOf("const boot = HcrmI18n.bootPlan(");
  const end = appSrc.indexOf("Vue.createApp({");
  assert.ok(start !== -1 && end > start, "the boot block markers moved");
  const bootSrc = appSrc.slice(start, end);
  assert.ok(!bootSrc.includes("Vue.createApp"), "the slice must end before mount");

  function runBoot({ search = "", stored = null, pathname = "/", hash = "", deny = false } = {}) {
    const writes = [], replaces = [], applied = [];
    const storage = {
      getItem: () => { if (deny) throw new Error("denied"); return stored; },
      setItem: (key, value) => {
        if (deny) throw new Error("denied");   // a truly blocked store
        writes.push([key, value]);
      },
    };
    const location = { search, pathname, hash };
    const history = { state: null, replaceState: (s, t, url) => replaces.push(url) };
    const shim = { bootPlan, applyLang: (lang) => { applied.push(lang); return lang; } };
    const fn = new Function("HcrmI18n", "location", "localStorage", "history", "LANG_KEY",
      `"use strict";\n${bootSrc}\nreturn boot;`);
    return { plan: fn(shim, location, storage, history, "hcrm_lang"), writes, replaces, applied };
  }

  const first = runBoot();
  assert.equal(first.plan.lang, "en");
  assert.deepEqual(first.writes, [], "a first visit writes NOTHING");
  assert.deepEqual(first.replaces, [], "and rewrites no URL");
  assert.deepEqual(first.applied, ["en"], "the language is still applied before mount");

  const linked = runBoot({ search: "?x=1&lang=da", hash: "#/catalogue", stored: "en" });
  assert.equal(linked.plan.lang, "da", "the query beats the stored preference");
  assert.deepEqual(linked.writes, [["hcrm_lang", "da"]], "exactly one write");
  assert.deepEqual(linked.replaces, ["/?x=1#/catalogue"],
    "the hash survives and unknown params are preserved");

  const denied = runBoot({ search: "?lang=da", deny: true });
  assert.equal(denied.plan.lang, "da", "a storage-blocked device keeps the query language");
  assert.deepEqual(denied.writes, []);
  assert.deepEqual(denied.replaces, ["/"], "and the strip still happens");

  const junk = runBoot({ search: "?lang=fr", stored: "en" });
  assert.equal(junk.plan.lang, "en");
  assert.deepEqual(junk.writes, [], "an unsupported language is stripped, never persisted");
  assert.deepEqual(junk.replaces, ["/"]);
});
