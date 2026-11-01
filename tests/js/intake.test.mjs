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
