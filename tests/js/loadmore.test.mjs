// Behaviour tests for the catalogue fetch generation guard (static/app.js
// reloadItems/loadMore), extracted from the REAL source — the interleavings
// the round-2 PR #34 review found (stale resolve, stale REJECT, semantic
// toggle mid-flight) are exactly the class a source-string pin cannot see.
//
// The methods are eval'd out of static/app.js against a scripted mock api(),
// so a regression in the shipped source fails here (node --test, no DOM).

import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = readFileSync(join(ROOT, "static", "app.js"), "utf8");

function extractMethod(header) {
  // brace-matched extraction: the header is the definition text ending
  // in " {" so a call site can never match (same discipline as the
  // pytest _method_body helper in tests/test_frontend_contract.py)
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
  throw new Error(`unbalanced braces after ${header}`);
}

const loadMoreSrc = extractMethod("async loadMore() {");
const reloadItemsSrc = extractMethod("async reloadItems() {");

const PAGE_SIZE = 12;

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeState(api) {
  const state = {
    loading: false, hasMore: true, items: [], offset: 0,
    search: "", semantic: false, category: "",
    _itemsGen: 0, toasts: [],
    showToast(msg) { this.toasts.push(msg); },
  };
  const loadMore = new Function(
    "api", "PAGE_SIZE", `"use strict"; return ({ ${loadMoreSrc} }).loadMore;`)(api, PAGE_SIZE);
  const reloadItems = new Function(
    "PAGE_SIZE", `"use strict"; return ({ ${reloadItemsSrc} }).reloadItems;`)(PAGE_SIZE);
  state.loadMore = loadMore.bind(state);
  state.reloadItems = reloadItems.bind(state);
  return state;
}

// A mock api() with per-call scripting: each entry is either a deferred
// (the caller controls when it settles) or a plain value/error.
function scriptedApi(script) {
  const calls = [];
  const api = (url) => {
    calls.push(url);
    const step = script[Math.min(calls.length - 1, script.length - 1)];
    if (step instanceof Error) return Promise.reject(step);
    if (step && step.deferred) return step.deferred.promise;
    return Promise.resolve(step);
  };
  api.calls = calls;
  return api;
}

test("stale RESOLVE is dropped and refetched with the fresh category", async () => {
  const d1 = deferred();
  const api = scriptedApi([{ deferred: d1 }, [{ id: 2, cat: "Kitchen" }]]);
  const s = makeState(api);
  s.semantic = true;
  s.search = "milk";

  const inflight = s.loadMore();
  assert.strictEqual(api.calls.length, 1);
  assert.ok(api.calls[0].startsWith("/items/vector-search?q=milk"));

  s.category = "Kitchen";          // user changes the filter mid-flight
  await s.reloadItems();           // bumps the generation; loadMore early-returns
  d1.resolve([{ id: 1 }]);         // the STALE response lands

  await inflight;
  assert.strictEqual(api.calls.length, 2);               // refetched, not just dropped
  assert.ok(api.calls[1].includes("category=Kitchen"));  // with the FRESH filter
  assert.deepStrictEqual(s.items, [{ id: 2, cat: "Kitchen" }]);
  assert.strictEqual(s.hasMore, false);
  assert.strictEqual(s.loading, false);
  assert.deepStrictEqual(s.toasts, []);
});

test("stale REJECT also refetches — no silent empty grid (round-2 N1)", async () => {
  const d1 = deferred();
  const api = scriptedApi([{ deferred: d1 }, [{ id: 9 }]]);
  const s = makeState(api);
  s.semantic = true;
  s.search = "milk";

  const inflight = s.loadMore();
  s.category = "Kitchen";
  await s.reloadItems();
  d1.reject(new Error("network blip"));   // the stale request ERRORS

  await inflight;
  // The fresh reload must still happen: refetch, commit, no toast (the
  // error belonged to the superseded request, not the current one).
  assert.strictEqual(api.calls.length, 2);
  assert.ok(api.calls[1].includes("category=Kitchen"));
  assert.deepStrictEqual(s.items, [{ id: 9 }]);
  assert.strictEqual(s.loading, false);
  assert.deepStrictEqual(s.toasts, []);
});

test("a CURRENT error toasts and stops paging", async () => {
  const api = scriptedApi([new Error("boom")]);
  const s = makeState(api);
  await s.loadMore();
  assert.deepStrictEqual(s.toasts, ["boom"]);
  assert.strictEqual(s.hasMore, false);
  assert.strictEqual(s.loading, false);
  assert.deepStrictEqual(s.items, []);
});

test("semantic toggle mid-flight refetches on the keyword path", async () => {
  const d1 = deferred();
  const api = scriptedApi([{ deferred: d1 }, Array.from({ length: PAGE_SIZE }, (_, i) => ({ id: i }))]);
  const s = makeState(api);
  s.semantic = true;
  s.search = "milk";

  const inflight = s.loadMore();
  s.semantic = false;              // toggle mid-flight
  await s.reloadItems();
  d1.resolve([{ id: 1 }]);

  await inflight;
  assert.strictEqual(api.calls.length, 2);
  assert.ok(api.calls[1].startsWith("/items?"));
  assert.ok(api.calls[1].includes("limit=12"));
  assert.ok(api.calls[1].includes("offset=0"));
  assert.strictEqual(s.items.length, PAGE_SIZE);
  assert.strictEqual(s.hasMore, true);   // full page: sentinel stays armed
});

test("double reload chases the generation without spinning or duplicate commits", async () => {
  const d1 = deferred();
  const api = scriptedApi([{ deferred: d1 }, [{ id: "final" }]]);
  const s = makeState(api);
  s.semantic = true;
  s.search = "mil";

  const inflight = s.loadMore();
  s.search = "milk";
  await s.reloadItems();           // gen bump #1 (loadMore busy: early-return)
  s.category = "Dairy";
  await s.reloadItems();           // gen bump #2
  d1.resolve([{ id: "stale" }]);

  await inflight;
  assert.strictEqual(api.calls.length, 2);            // exactly one refetch
  assert.ok(api.calls[1].includes("category=Dairy")); // the NEWEST state wins
  assert.deepStrictEqual(s.items, [{ id: "final" }]);
  assert.strictEqual(s.loading, false);
});
