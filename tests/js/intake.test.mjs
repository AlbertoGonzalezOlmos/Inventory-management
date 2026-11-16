// Slice D behaviour test — the scan-intake analysis line.
//
// The line is a mixture: barcode-format/region/usage values are MACHINE data
// (never translated), while the restricted-circulation note is copy and must
// follow the UI language. The method is eval'd from the REAL static/app.js.

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
  throw new Error(`unbalanced braces after ${header}`);
}

function intakeObject(lang, analysis) {
  const methods = new Function("HcrmI18n",
    `"use strict"; return ({ ${extractMethod("t(id, vars) {")},` +
    `\n${extractMethod("intakeAnalysisLine() {")} });`)(HcrmI18n);
  return { lang, scanIntake: { analysis }, ...methods };
}

test("machine analysis values stay put and the note follows the language", () => {
  const analysis = { format: "EAN-13", region: "Spain & Andorra",
                     usage: "restricted-region", globally_unique: false };
  const en = intakeObject("en", analysis);
  const da = intakeObject("da", analysis);
  assert.equal(en.intakeAnalysisLine(),
    "EAN-13 · Spain & Andorra · restricted-region · restricted circulation");
  assert.equal(da.intakeAnalysisLine(),
    "EAN-13 · Spain & Andorra · restricted-region · begrænset cirkulation");
  assert.equal(intakeObject("en", { format: "GTIN" }).intakeAnalysisLine(), "GTIN",
    "no restricted note for a globally unique code");
});

// Execute the real staff-intake methods, not a copy of their control flow. The
// requester's cart is the resolve endpoint's business; the staff member's own
// cart must be touched only by a normal (not request-originated) intake.
const STAFF_BRANCHES = [
  { name: "pre-check existing", branch: "precheck", addToCart: false,
    toast: "intake.already_filed" },
  { name: "409 concurrent-create fallback", branch: "conflict", addToCart: true,
    toast: "intake.already_filed" },
  { name: "newly created", branch: "new", addToCart: false,
    toast: "intake.added" },
];

function staffIntakeHarness(branch, {
  resolveRequestId = null, addToCart = true, openRequests = [],
} = {}) {
  const calls = [];
  const item = { id: 900, sku: "EX-1", name: "Thing" };
  const replacementCart = { lines: [{ item_id: item.id, qty: 1 }], total_qty: 1 };
  const originalCart = { lines: [{ item_id: 17, qty: 2 }], total_qty: 2 };

  async function api(path, options = {}) {
    const method = options.method || "GET";
    calls.push({ path, method, body: options.body });
    if (path.startsWith("/items?barcode=")) {
      if (branch === "precheck") return [item];
      const lookups = calls.filter(c => c.path.startsWith("/items?barcode=")).length;
      return branch === "conflict" && lookups > 1 ? [item] : [];
    }
    if (path === "/items" && method === "POST") {
      if (branch === "conflict") {
        throw Object.assign(new Error("already filed"), { status: 409 });
      }
      return item;
    }
    if (path === "/cart/items" && method === "POST") return replacementCart;
    if (path === "/scan-requests?status=open") return openRequests;
    if (/^\/scan-requests\/\d+\/resolve$/.test(path) && method === "POST") return {};
    throw new Error(`unexpected API call: ${method} ${path}`);
  }

  const methods = new Function("api",
    `"use strict"; return ({\n${[
      "async submitIntakeStaff(intake) {",
      "async _intakeAddToCart(itemId) {",
      "async _resolveIfRequested(intake, itemId) {",
    ].map(extractMethod).join(",\n")}\n});`)(api);
  const state = {
    ...methods,
    cart: originalCart,
    currentView: "admin",
    toasts: [],
    closed: 0,
    adminLoads: 0,
    scanRequestLoads: 0,
    showToast(descriptor) { this.toasts.push(descriptor); },
    closeIntake() { this.closed++; },
    loadAdminData() { this.adminLoads++; },
    loadScanRequests() { this.scanRequestLoads++; },
  };
  const intake = {
    code: "8412345678905",
    catalogable: true,
    analysis: { gtin14: "8412345678905" },
    price: "1.25",
    sku: "EX-1",
    name: "Thing",
    resolveRequestId,
    addToCart,
  };
  return { state, intake, calls, item, replacementCart, originalCart };
}

function toastKeys(state) {
  return state.toasts.map(descriptor => descriptor.key);
}

test("member request resolution never POSTs to the staff cart (all three item branches)", async t => {
  for (const fixture of STAFF_BRANCHES) {
    await t.test(fixture.name, async () => {
      const h = staffIntakeHarness(fixture.branch, {
        resolveRequestId: 42, addToCart: fixture.addToCart,
      });
      await h.state.submitIntakeStaff(h.intake);

      assert.equal(h.calls.filter(c => c.path === "/cart/items").length, 0,
        `request resolution mutated the staff cart: ${JSON.stringify(h.calls)}`);
      assert.equal(h.state.cart, h.originalCart, "the staff cart reference is untouched");
      const resolves = h.calls.filter(c => c.path === "/scan-requests/42/resolve");
      assert.deepEqual(resolves.map(c => ({ method: c.method, body: c.body })), [{
        method: "POST",
        body: { item_id: h.item.id, add_to_cart: fixture.addToCart },
      }], "the requester's add_to_cart choice survives");
      assert.deepEqual(toastKeys(h.state), ["intake.resolved"],
        "no toast may falsely claim a staff purchase-list mutation");
      assert.equal(h.state.closed, 1);
      assert.equal(h.state.scanRequestLoads, 1);
      assert.equal(h.state.adminLoads, fixture.branch === "new" ? 1 : 0,
        "early returns keep their tail; new-item admin refresh stays last");
    });
  }
});

test("normal staff intake adds to the staff cart exactly once (all three item branches)", async t => {
  for (const fixture of STAFF_BRANCHES) {
    await t.test(fixture.name, async () => {
      const h = staffIntakeHarness(fixture.branch);
      await h.state.submitIntakeStaff(h.intake);

      const adds = h.calls.filter(c => c.path === "/cart/items");
      assert.deepEqual(adds.map(c => ({ method: c.method, body: c.body })), [{
        method: "POST", body: { item_id: h.item.id, qty: 1 },
      }]);
      assert.equal(h.state.cart, h.replacementCart);
      assert.equal(h.calls.filter(c => c.path.endsWith("/resolve")).length, 0);
      assert.deepEqual(toastKeys(h.state), [fixture.toast]);
      assert.equal(h.state.closed, 1);
      assert.equal(h.state.scanRequestLoads, 1);
      assert.equal(h.state.adminLoads, fixture.branch === "new" ? 1 : 0);
    });
  }
});

test("normal staff intake with resolved_also still adds once to each intended cart", async () => {
  const h = staffIntakeHarness("new", {
    openRequests: [{ id: 7, code: "8412345678905", requester: { name: "Member" } }],
  });
  await h.state.submitIntakeStaff(h.intake);

  const adds = h.calls.filter(c => c.path === "/cart/items");
  assert.equal(adds.length, 1, "a normal staff intake keeps its own cart add");
  assert.deepEqual(adds[0].body, { item_id: h.item.id, qty: 1 });
  const resolves = h.calls.filter(c => c.path === "/scan-requests/7/resolve");
  assert.deepEqual(resolves.map(c => c.body), [{ item_id: h.item.id, add_to_cart: true }]);
  assert.deepEqual(toastKeys(h.state), ["intake.added", "intake.resolved_also"]);
  assert.equal(h.state.cart, h.replacementCart);
  assert.equal(h.state.closed, 1);
  assert.equal(h.state.adminLoads, 1);
});
