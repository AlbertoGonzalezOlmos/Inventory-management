// Behaviour pins for the forced-password-change path, evaluated from the
// shipped methods (not a source-text-only assertion): normal changes stay on
// Account; a flagged account enters a loaded catalogue.

import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = readFileSync(join(ROOT, "static", "app.js"), "utf8");

function extractMethod(header) {
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

const enterSrc = extractMethod("async _enterCatalogue() {");
const changeSrc = extractMethod("async changePassword() {");

function makeState(flagged) {
  const calls = [];
  const location = { hash: "#/account" };
  const storage = { setItem() {} };
  const state = {
    user: { must_change_password: flagged },
    pwForm: { current_password: "old", new_password: "new" },
    pwError: "", pwOk: false, busy: false,
    route: "#/account",
    async loadCart() { calls.push("cart"); },
    _announceCartRestored() { calls.push("announce"); },
    async replayStagedScans() { calls.push("replay"); },
    reloadItems() { calls.push("items"); },
    loadCategories() { calls.push("categories"); },
    showToast() {},
    errDescriptor(error) { return String(error); },
  };
  state._enterCatalogue = new Function(
    "location", `"use strict"; return ({ ${enterSrc} })._enterCatalogue;`,
  )(location).bind(state);
  const api = async () => ({});
  state.changePassword = new Function(
    "api", "USER_KEY", "localStorage",
    `"use strict"; return ({ ${changeSrc} }).changePassword;`,
  )(api, "hcrm_user", storage).bind(state);
  return { state, calls, location };
}

test("forced password change enters catalogue with cart, items and categories", async () => {
  const { state, calls, location } = makeState(true);
  await state.changePassword();
  assert.equal(state.user.must_change_password, false);
  assert.equal(state.route, "#/catalogue");
  assert.equal(location.hash, "#/catalogue");
  assert.deepStrictEqual(calls,
    ["cart", "announce", "replay", "items", "categories"]);
  assert.equal(state.pwOk, true);
  assert.equal(state.busy, false);
});

test("normal password change stays on Account and does not load catalogue", async () => {
  const { state, calls, location } = makeState(false);
  await state.changePassword();
  assert.equal(state.route, "#/account");
  assert.equal(location.hash, "#/account");
  assert.deepStrictEqual(calls, []);
  assert.equal(state.pwOk, true);
  assert.equal(state.busy, false);
});
