// Session-expiry rejection tests — `node --test tests/js/`.
//
// The bug: api()'s 401 branch clears the session and redirects, then throws to
// stop the caller's continuation. A boot/route load with no catch (the initial
// cart load) left that throw as an uncaught promise rejection, which the
// browser gate treats as a hard failure.
//
// We execute the SHIPPED functions, extracted from static/app.js (the repo's
// brace-extraction idiom), with fake fetch/storage/location/window:
//   (a) the 401 branch marks the error, clears tokens, dispatches the teardown
//       and redirects — while a login 401 is NOT the session-expiry signal;
//   (b) the window guard preventDefaults ONLY the flagged rejection;
//   (c) an ordinary 500 rejection still propagates (and is not silenced);
//   (d) loadCart swallows only the flagged rejection and rethrows real ones.

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

const TOKEN_KEY = "hcrm_token";
const USER_KEY = "hcrm_user";

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

function buildApiError(storage, location) {
  return new Function(
    "HcrmI18n", "localStorage", "location",
    `"use strict"; ${extractMethod("function apiError(res, data, fallbackCode) {")}; return apiError;`,
  )(HcrmI18n, storage, location);
}

function buildApi({ storage, location, windowStub, fetchImpl }) {
  const apiError = buildApiError(storage, location);
  const tGlobal = (id) => HcrmI18n.translate(id, null, HcrmI18n.DEFAULT_LANG);
  return new Function(
    "HcrmI18n", "localStorage", "location", "window", "fetch",
    "TOKEN_KEY", "USER_KEY", "API_BASE", "tGlobal", "apiError",
    `"use strict"; ${extractMethod('async function api(path, { method = "GET", body, raw = false, token } = {}) {')}; return api;`,
  )(HcrmI18n, storage, location, windowStub, fetchImpl,
    TOKEN_KEY, USER_KEY, "/api", tGlobal, apiError);
}

function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    removed: [],
    has(key) { return data.has(key); },
    getItem(key) { return data.has(key) ? data.get(key) : null; },
    setItem(key, value) { data.set(key, String(value)); },
    removeItem(key) { this.removed.push(key); data.delete(key); },
  };
}

function fakeWindow() {
  return {
    events: [],
    dispatchEvent(event) { this.events.push(event.type); },
  };
}

function response(status, body = null) {
  return {
    status,
    headers: { get: () => null },
    json: async () => body,
  };
}

function buildGuard() {
  return new Function(
    `"use strict"; ${extractMethod("function isSessionExpiredRejection(reason) {")}
     ${extractMethod("function installSessionExpiredGuard(target) {")}
     return installSessionExpiredGuard;`,
  )();
}

function dispatchGuard(install, reason) {
  const listeners = {};
  install({ addEventListener(type, fn) { listeners[type] = fn; } });
  assert.equal(typeof listeners.unhandledrejection, "function",
    "the guard must listen for unhandledrejection");
  const event = {
    reason,
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
  listeners.unhandledrejection(event);
  return event.prevented;
}

// --- (a) the 401 branch -----------------------------------------------------

test("a 401 marks the error and still clears, dispatches the teardown and redirects", async () => {
  const storage = fakeStorage({ [TOKEN_KEY]: "bogus", [USER_KEY]: '{"id":1}' });
  const location = { hash: "#/catalogue", search: "" };
  const win = fakeWindow();
  const api = buildApi({
    storage, location, windowStub: win,
    fetchImpl: async () => response(401),
  });

  let error = null;
  try { await api("/items"); } catch (e) { error = e; }
  assert.ok(error, "the 401 branch must still reject");
  assert.equal(error.status, 401, "the caller keeps the HTTP status");
  assert.equal(error.sessionExpired, true, "the control-flow flag is set");
  assert.equal(error.message,
    HcrmI18n.translate("auth.session_expired", null, HcrmI18n.DEFAULT_LANG),
    "the translated session-expired message is preserved");

  assert.deepEqual(storage.removed.sort(), [TOKEN_KEY, USER_KEY].sort(),
    "both the token and the cached user are cleared");
  assert.equal(storage.has(TOKEN_KEY), false);
  assert.equal(storage.has(USER_KEY), false);
  assert.deepEqual(win.events, ["hcrm-session-expired"],
    "the canonical teardown is dispatched");
  assert.equal(location.hash, "#/login", "the browser is sent to the login route");
});

test("a login 401 is NOT the session-expiry signal (tokens/route untouched)", async () => {
  const storage = fakeStorage({ [TOKEN_KEY]: "still-valid", [USER_KEY]: '{"id":1}' });
  const location = { hash: "#/catalogue", search: "" };
  const win = fakeWindow();
  const api = buildApi({
    storage, location, windowStub: win,
    fetchImpl: async () => response(401, { detail: "Bad credentials" }),
  });

  let error = null;
  try { await api("/auth/login"); } catch (e) { error = e; }
  assert.ok(error);
  assert.equal(error.status, 401);
  assert.ok(!error.sessionExpired, "a failed login must surface its own error");
  assert.deepEqual(storage.removed, []);
  assert.deepEqual(win.events, []);
  assert.equal(location.hash, "#/catalogue");
});

// --- (b) the window guard ---------------------------------------------------

test("the guard preventDefaults ONLY the sessionExpired flag", () => {
  const install = buildGuard();
  assert.equal(dispatchGuard(install,
    Object.assign(new Error("expired"), { sessionExpired: true })), true);

  // Everything else must still reach the console gate.
  assert.equal(dispatchGuard(install, new Error("database down")), false);
  assert.equal(dispatchGuard(install,
    Object.assign(new Error("unauthorized"), { status: 401 })), false,
  "a bare status is not the signal");
  assert.equal(dispatchGuard(install, { sessionExpired: "yes" }), false,
    "only the boolean true counts");
  assert.equal(dispatchGuard(install, { sessionExpired: false }), false);
  assert.equal(dispatchGuard(install, null), false);
  assert.equal(dispatchGuard(install, "uncaught string"), false);
});

// --- (c) an ordinary 500 ----------------------------------------------------

test("an ordinary 500 still rejects and the guard does not silence it", async () => {
  const storage = fakeStorage({ [TOKEN_KEY]: "still-valid", [USER_KEY]: '{"id":1}' });
  const location = { hash: "#/catalogue", search: "" };
  const win = fakeWindow();
  const api = buildApi({
    storage, location, windowStub: win,
    fetchImpl: async () => response(500, { detail: "Database unavailable" }),
  });

  let error = null;
  try { await api("/items"); } catch (e) { error = e; }
  assert.ok(error);
  assert.equal(error.status, 500);
  assert.ok(!error.sessionExpired);
  assert.equal(error.message, "Database unavailable");
  assert.deepEqual(storage.removed, [], "a 500 never clears the session");
  assert.deepEqual(win.events, [], "a 500 never dispatches the teardown");
  assert.equal(location.hash, "#/catalogue");
  assert.equal(dispatchGuard(buildGuard(), error), false,
    "the 500 rejection must still be reported");
});

// --- (d) the shipped loadCart call site -------------------------------------

test("the SHIPPED loadCart swallows the flag and rethrows a real failure", async () => {
  const isSessionExpiredRejection = new Function(
    `"use strict"; ${extractMethod("function isSessionExpiredRejection(reason) {")}; return isSessionExpiredRejection;`,
  )();
  const loadCart = (apiImpl) => new Function(
    "api", "isSessionExpiredRejection",
    `"use strict"; return ({${extractMethod("async loadCart() {")}}).loadCart;`,
  )(apiImpl, isSessionExpiredRejection);

  const vm = {
    user: { id: 1, must_change_password: false },
    cart: { lines: [{ item: { id: 1 }, qty: 1 }], total_qty: 1, total_cents: 100 },
    _cartImportedLegacy: false,
    importLegacyPurchase: async () => false,
  };

  const expired = Object.assign(new Error("Sessionen er udløbet"), {
    status: 401, sessionExpired: true,
  });
  await loadCart(async () => { throw expired; }).call(vm);
  assert.deepEqual(vm.cart, { lines: [{ item: { id: 1 }, qty: 1 }], total_qty: 1, total_cents: 100 },
    "the teardown owns the cart; loadCart leaves it alone and resolves");

  const real = Object.assign(new Error("Database unavailable"), { status: 500 });
  await assert.rejects(loadCart(async () => { throw real; }).call(vm), /Database unavailable/,
    "a real failure must still reach the caller (and therefore the console gate)");
});
