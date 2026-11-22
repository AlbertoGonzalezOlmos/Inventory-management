// Scan-feed reducer tests (round 4, T6) — `node --test tests/js/`.
//
// The reducer (static/scanfeed.js) is pure: no DOM, no timers, no fetch.
// These tests drive it with scripted poll results and assert the EFFECT
// sequences — the adapter's exact instructions (startPoll / stopPoll /
// emitEvent / setPill) — plus the cursor arithmetic the at-most-once
// delivery stands on. The DOM/fetch side of every assertion is exercised
// by the adapter contract pins (tests/test_frontend_contract.py) and
// ui_check's rendered data-scanner-feed pill.

import test from "node:test";
import assert from "node:assert/strict";

import HcrmScanFeed from "../../static/scanfeed.js";
import MESSAGES from "../../static/i18n.messages.js";

const {
  POLL_INTERVAL_MS, UNPAIRED, POLLING, ERROR,
  PILL_LIVE, PILL_OFF, PILL_UNPAIRED,
  initialFeedState, feedStep,
} = HcrmScanFeed;

function event(seq, code, kind = "barcode") {
  return { seq, code, kind };
}

function effectsOf(result) {
  return result.effects.map((e) => e.type);
}

// --- the client-pacing constant (the no-hold rule's other half) ---------------

test("POLL_INTERVAL_MS is the paced 1s poll, and it is a client constant", () => {
  // It is never a request parameter (that would be a server hold —
  // ui_check's virtual-time gate would hang); the value is pinned here AND
  // mirrored into the pytest contract suite.
  assert.equal(POLL_INTERVAL_MS, 1000);
});

// --- initial + paired ------------------------------------------------------------

test("initial state is unpaired with no token and cursor 0", () => {
  const s = initialFeedState();
  assert.equal(s.mode, UNPAIRED);
  assert.equal(s.token, null);
  assert.equal(s.cursor, 0);
});

test("paired(token): polling from cursor 0, poll started (live deferred to first poll)", () => {
  const result = feedStep(initialFeedState(), { type: "paired", token: "T" });
  assert.deepEqual(result.state, { mode: POLLING, cursor: 0, token: "T",
    detail: null, reclaimUsed: false });
  // Token issuance is not streaming proof: no immediate setPill live.
  assert.deepEqual(result.effects, [{ type: "startPoll" }]);
});

test("paired from any state (re-pair after error, after unpair)", () => {
  for (const prior of [POLLING, ERROR, UNPAIRED]) {
    const state = { mode: prior, cursor: 42, token: prior === UNPAIRED ? null : "old" };
    const result = feedStep(state, { type: "paired", token: "new" });
    // cursor resets BY DESIGN: the server's consumed-marking is the
    // cross-restart dedupe; the cursor is within-session paging.
    assert.deepEqual(result.state, { mode: POLLING, cursor: 0, token: "new",
      detail: null, reclaimUsed: false });
    assert.ok(result.effects.some((e) => e.type === "startPoll"));
  }
});

// --- events: emission, cursor advance, no double-emit ------------------------------

test("events are emitted in order and the cursor advances to latest_seq", () => {
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  const result = feedStep(state, {
    type: "events",
    events: [event(3, "8412345678905"), event(4, "HCRM1:AAA", "badge")],
    latest_seq: 4,
  });
  assert.equal(result.state.mode, POLLING);
  assert.equal(result.state.cursor, 4);
  assert.deepEqual(result.effects, [
    { type: "emitEvent", code: "8412345678905", kind: "barcode" },
    { type: "emitEvent", code: "HCRM1:AAA", kind: "badge" },
    { type: "setPill", mode: PILL_LIVE },
    { type: "setDetail", reason: null },
  ]);
});

test("no double-emit: events with seq <= cursor are dropped", () => {
  // A replayed/stale response (or the server re-delivering) must not emit
  // anything the cursor has already passed.
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  state = feedStep(state, { type: "events",
    events: [event(3, "A"), event(4, "B")], latest_seq: 4 }).state;
  const result = feedStep(state, {
    type: "events",
    events: [event(3, "A"), event(4, "B"), event(5, "C")],
    latest_seq: 5,
  });
  assert.deepEqual(result.effects, [
    { type: "emitEvent", code: "C", kind: "barcode" },
    { type: "setPill", mode: PILL_LIVE },
    { type: "setDetail", reason: null },
  ]);
  assert.equal(result.state.cursor, 5);
});

test("cursor is monotonic: a post-prune latest_seq regression cannot rewind it", () => {
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  state = feedStep(state, { type: "events", events: [event(9, "A")], latest_seq: 9 }).state;
  const result = feedStep(state, { type: "events", events: [], latest_seq: 0 });
  assert.equal(result.state.cursor, 9);   // never regresses
  assert.deepEqual(result.effects, [
    { type: "setPill", mode: PILL_LIVE },
    { type: "setDetail", reason: null },
  ]);
});

test("cursor respects the event seqs even when latest_seq lies low", () => {
  const state = { mode: POLLING, cursor: 0, token: "T" };
  const result = feedStep(state, {
    type: "events", events: [event(7, "A")], latest_seq: 0,
  });
  assert.equal(result.state.cursor, 7);
});

test("an empty poll emits nothing but the pill", () => {
  const state = { mode: POLLING, cursor: 0, token: "T" };
  const result = feedStep(state, { type: "events", events: [], latest_seq: 0 });
  assert.deepEqual(result.effects, [
    { type: "setPill", mode: PILL_LIVE },
    { type: "setDetail", reason: null },
  ]);
});

test("events racing an unpair are ignored — no double-emit after unbind", () => {
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  state = feedStep(state, { type: "unpair" }).state;
  const result = feedStep(state, {
    type: "events", events: [event(1, "A")], latest_seq: 1,
  });
  assert.equal(result.state.mode, UNPAIRED);
  assert.deepEqual(result.effects, []);
});

// --- errors: retry state, not a stop ------------------------------------------------

test("pollError flips to error with pill off, keeping token and cursor", () => {
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  state = feedStep(state, { type: "events", events: [event(3, "A")], latest_seq: 3 }).state;
  const result = feedStep(state, { type: "pollError" });
  assert.equal(result.state.mode, ERROR);
  assert.equal(result.state.token, "T");
  assert.equal(result.state.cursor, 3);       // retry resumes where it left off
  assert.equal(result.state.detail, "server-down");
  assert.deepEqual(result.effects, [
    { type: "setPill", mode: PILL_OFF },
    { type: "setDetail", reason: "server-down" },
  ]);
  // NOTE: no stopPoll — the adapter's timer keeps firing (self-healing).
  assert.ok(!result.effects.some((e) => e.type === "stopPoll"));
});

test("a successful poll recovers from error (pill live again)", () => {
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  state = feedStep(state, { type: "pollError" }).state;
  const result = feedStep(state, { type: "events", events: [], latest_seq: 0 });
  assert.equal(result.state.mode, POLLING);
  assert.deepEqual(result.effects, [
    { type: "setPill", mode: PILL_LIVE },
    { type: "setDetail", reason: null },
  ]);
});

test("pollError while unpaired is inert", () => {
  const result = feedStep(initialFeedState(), { type: "pollError" });
  assert.equal(result.state.mode, UNPAIRED);
  assert.deepEqual(result.effects, []);
});

// --- unpair ---------------------------------------------------------------------------

test("unpair resets everything and stops the poll", () => {
  for (const prior of [POLLING, ERROR]) {
    const state = { mode: prior, cursor: 12, token: "T" };
    const result = feedStep(state, { type: "unpair" });
    assert.deepEqual(result.state, initialFeedState());
    assert.deepEqual(result.effects, [
      { type: "stopPoll" },
      { type: "setPill", mode: PILL_UNPAIRED },
      { type: "setDetail", reason: null },
    ]);
  }
});

// --- hygiene ---------------------------------------------------------------------------

test("unknown actions are inert, never a crash", () => {
  const state = initialFeedState();
  const result = feedStep(state, { type: "nonsense" });
  assert.equal(result.state, state);
  assert.deepEqual(result.effects, []);
});

test("events fields are optional-tolerant (undefined list / latest_seq)", () => {
  const state = { mode: POLLING, cursor: 0, token: "T" };
  const result = feedStep(state, { type: "events" });
  assert.equal(result.state.cursor, 0);
  assert.deepEqual(result.effects, [
    { type: "setPill", mode: PILL_LIVE },
    { type: "setDetail", reason: null },
  ]);
});

// --- P3: popup handshake actions + failure detail ----------------------------

const { DETAILS } = HcrmScanFeed;
const AGENT_PRESENT = { online: true, device_status: "present",
                        personality: "hid-a001" };

function pairedState() {
  return feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
}
function pillOf(result) {
  return result.effects.find((e) => e.type === "setPill").mode;
}
function detailOf(result) {
  return result.effects.find((e) => e.type === "setDetail").reason;
}

test("DETAILS is exactly the R5 detail enum", () => {
  assert.deepEqual([...DETAILS].sort(), [
    "bound-elsewhere", "no-agent", "no-device", "no-permission",
    "popup-blocked", "register-denied", "server-down",
  ]);
});

test("popup: opens the popup, arms the watchdog, resets the reclaim budget", () => {
  const prior = { ...pairedState(), reclaimUsed: true, detail: "no-device" };
  const result = feedStep(prior, { type: "popup", reclaim: 0 });
  assert.equal(result.state.mode, UNPAIRED);
  assert.equal(result.state.detail, null);
  assert.equal(result.state.reclaimUsed, false);
  assert.equal(result.state.cursor, prior.cursor);   // cursor never reset here
  assert.deepEqual(result.effects, [
    { type: "openPopup", reclaim: 0 },
    { type: "armWatchdog" },
  ]);
});

test("offer: issues a pairRequest and clears the watchdog, never a token", () => {
  const result = feedStep(pairedState(), {
    type: "offer", nonce: "N", terminal_id: "counter-1", code: "7Q2MB4XW",
  });
  assert.equal(result.state.token, "T");            // unchanged until /pair
  assert.deepEqual(result.effects, [
    { type: "pairRequest", terminal_id: "counter-1", code: "7Q2MB4XW" },
    { type: "clearWatchdog" },
  ]);
});

test("pairFailed: every R5 detail maps to setDetail + closePopup (paired => off)", () => {
  for (const reason of DETAILS) {
    const result = feedStep(pairedState(), { type: "pairFailed", reason });
    assert.equal(result.state.detail, reason);
    assert.equal(pillOf(result), PILL_OFF);
    assert.equal(detailOf(result), reason);
    assert.ok(result.effects.some((e) => e.type === "closePopup"));
    assert.ok(result.effects.some((e) => e.type === "clearWatchdog"));
  }
});

test("pairFailed while unpaired keeps the pill unpaired (wedge fallback)", () => {
  const result = feedStep(initialFeedState(),
    { type: "pairFailed", reason: "register-denied" });
  assert.equal(result.state.mode, UNPAIRED);
  assert.equal(pillOf(result), PILL_UNPAIRED);
  assert.equal(detailOf(result), "register-denied");
});

test("pairFailed with an unknown reason is inert", () => {
  const state = pairedState();
  const result = feedStep(state, { type: "pairFailed", reason: "bogus" });
  assert.equal(result.state, state);
  assert.deepEqual(result.effects, []);
});

test("reclaim: one attempt per popup session, then bound-elsewhere", () => {
  const first = feedStep(pairedState(), { type: "reclaim" });
  assert.equal(first.state.reclaimUsed, true);
  assert.deepEqual(first.effects, [
    { type: "openPopup", reclaim: 1 },
    { type: "armWatchdog" },
  ]);
  const second = feedStep(first.state, { type: "reclaim" });
  assert.equal(second.state.detail, "bound-elsewhere");
  assert.equal(pillOf(second), PILL_OFF);
  assert.ok(!second.effects.some((e) => e.type === "openPopup"));
});

// --- P3: live requires a fresh, present agent; degraded old-server leg --------

test("events with agent online + present => live, detail null", () => {
  const result = feedStep(pairedState(), {
    type: "events", events: [], latest_seq: 0, agent: AGENT_PRESENT,
  });
  assert.equal(pillOf(result), PILL_LIVE);
  assert.equal(detailOf(result), null);
  assert.equal(result.state.detail, null);
});

test("events with agent online but device absent => off + no-device", () => {
  const result = feedStep(pairedState(), {
    type: "events", events: [], latest_seq: 0,
    agent: { online: true, device_status: "absent", personality: null },
  });
  assert.equal(pillOf(result), PILL_OFF);
  assert.equal(detailOf(result), "no-device");
});

test("events with agent online but no-permission => off + no-permission", () => {
  const result = feedStep(pairedState(), {
    type: "events", events: [], latest_seq: 0,
    agent: { online: true, device_status: "no-permission", personality: "hid-a001" },
  });
  assert.equal(pillOf(result), PILL_OFF);
  assert.equal(detailOf(result), "no-permission");
});

test("events with agent offline => off + no-agent", () => {
  const result = feedStep(pairedState(), {
    type: "events", events: [], latest_seq: 0,
    agent: { online: false, device_status: "present", personality: "hid-a001" },
  });
  assert.equal(pillOf(result), PILL_OFF);
  assert.equal(detailOf(result), "no-agent");
});

test("events with a null agent => off + no-agent (not the legacy leg)", () => {
  const result = feedStep(pairedState(), {
    type: "events", events: [], latest_seq: 0, agent: null,
  });
  assert.equal(pillOf(result), PILL_OFF);
  assert.equal(detailOf(result), "no-agent");
});

test("DEGRADED old server: agent undefined => today's live on a successful poll", () => {
  const result = feedStep(pairedState(), {
    type: "events", events: [event(3, "A")], latest_seq: 3,   // no agent key
  });
  assert.equal(pillOf(result), PILL_LIVE);
  assert.equal(detailOf(result), null);
  assert.equal(result.state.cursor, 3);
  assert.deepEqual(result.effects[0], { type: "emitEvent", code: "A", kind: "barcode" });
});

test("a healthy poll clears a previously-set failure detail", () => {
  let state = feedStep(pairedState(), {
    type: "events", events: [], latest_seq: 0,
    agent: { online: true, device_status: "absent" },
  }).state;
  assert.equal(state.detail, "no-device");
  const result = feedStep(state, {
    type: "events", events: [], latest_seq: 0, agent: AGENT_PRESENT,
  });
  assert.equal(pillOf(result), PILL_LIVE);
  assert.equal(detailOf(result), null);
  assert.equal(result.state.detail, null);
});

// --- P3: every detail reason + handshake string has i18n copy (en + da) ------

test("every pill detail reason maps to a translated scanner.detail.* row", () => {
  const detailKeys = {
    "no-agent": "scanner.detail.no_agent",
    "no-device": "scanner.detail.no_device",
    "no-permission": "scanner.detail.no_permission",
    "bound-elsewhere": "scanner.detail.bound_elsewhere",
    "register-denied": "scanner.detail.register_denied",
    "server-down": "scanner.detail.server_down",
    "popup-blocked": "scanner.detail.popup_blocked",
  };
  for (const reason of DETAILS) {
    const key = detailKeys[reason];
    assert.ok(key, `no i18n key mapped for detail ${reason}`);
    assert.ok(MESSAGES.en[key], `${key} missing en`);
    assert.ok(MESSAGES.da[key], `${key} missing da`);
    assert.notEqual(MESSAGES.en[key], MESSAGES.da[key], `${key} da is a copy`);
  }
});

test("popup handshake strings exist with real Danish copy", () => {
  for (const key of ["scanner.pairing", "scanner.reclaim", "scanner.retry",
                     "scanner.offer_invalid", "scanner.agent_upgrade_required"]) {
    assert.ok(MESSAGES.en[key], `${key} missing en`);
    assert.ok(MESSAGES.da[key], `${key} missing da`);
    assert.notEqual(MESSAGES.en[key], MESSAGES.da[key], `${key} da is a copy`);
  }
});
