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

test("paired(token): polling from cursor 0, pill live, poll started", () => {
  const result = feedStep(initialFeedState(), { type: "paired", token: "T" });
  assert.deepEqual(result.state, { mode: POLLING, cursor: 0, token: "T" });
  assert.deepEqual(result.effects, [
    { type: "setPill", mode: PILL_LIVE },
    { type: "startPoll" },
  ]);
});

test("paired from any state (re-pair after error, after unpair)", () => {
  for (const prior of [POLLING, ERROR, UNPAIRED]) {
    const state = { mode: prior, cursor: 42, token: prior === UNPAIRED ? null : "old" };
    const result = feedStep(state, { type: "paired", token: "new" });
    // cursor resets BY DESIGN: the server's consumed-marking is the
    // cross-restart dedupe; the cursor is within-session paging.
    assert.deepEqual(result.state, { mode: POLLING, cursor: 0, token: "new" });
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
  ]);
  assert.equal(result.state.cursor, 5);
});

test("cursor is monotonic: a post-prune latest_seq regression cannot rewind it", () => {
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  state = feedStep(state, { type: "events", events: [event(9, "A")], latest_seq: 9 }).state;
  const result = feedStep(state, { type: "events", events: [], latest_seq: 0 });
  assert.equal(result.state.cursor, 9);   // never regresses
  assert.deepEqual(result.effects, [{ type: "setPill", mode: PILL_LIVE }]);
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
  assert.deepEqual(result.effects, [{ type: "setPill", mode: PILL_LIVE }]);
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
  assert.deepEqual(result.effects, [{ type: "setPill", mode: PILL_OFF }]);
  // NOTE: no stopPoll — the adapter's timer keeps firing (self-healing).
  assert.ok(!result.effects.some((e) => e.type === "stopPoll"));
});

test("a successful poll recovers from error (pill live again)", () => {
  let state = feedStep(initialFeedState(), { type: "paired", token: "T" }).state;
  state = feedStep(state, { type: "pollError" }).state;
  const result = feedStep(state, { type: "events", events: [], latest_seq: 0 });
  assert.equal(result.state.mode, POLLING);
  assert.deepEqual(result.effects, [{ type: "setPill", mode: PILL_LIVE }]);
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
  assert.deepEqual(result.effects, [{ type: "setPill", mode: PILL_LIVE }]);
});
