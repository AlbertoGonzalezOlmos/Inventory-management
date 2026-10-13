/* HCRM scan feed — the pure poll-channel state machine (round 4, T6).
 *
 * The round-4 grab daemon (scripts/scanner_daemon.py) forwards the Opticon
 * M-10's scans into a server-side per-terminal queue (POST /api/scanner/
 * events); THIS module decides what the SPA does with the poll results.
 * All DOM work — the setInterval pacing, the fetch, the header pill, the
 * localStorage of the terminal token — lives in the app.js adapter; this
 * file is PURE so `node --test` (tests/js/scanfeed.test.mjs) can drive it
 * with scripted poll results and assert the EFFECT sequences, which are
 * the adapter's exact instructions.
 *
 * States (mode):
 *   UNPAIRED — no terminal token; the wedge capture (round 3,
 *              scancapture.js) is the ONLY scan path. NOT an error: it is
 *              the fallback, deliberately kept installed.
 *   POLLING  — paired, the paced 1 s poll is running.
 *   ERROR    — paired but the last poll failed (server unreachable): the
 *              poll KEEPS RUNNING (a kiosk must heal itself); this state
 *              only flips the pill to "off".
 *
 * Actions: {type:"paired", token} · {type:"events", events, latest_seq} ·
 * {type:"pollError"} · {type:"unpair"}. Effects (the adapter's orders):
 * startPoll · stopPoll · emitEvent(code, kind) · setPill(mode) — pill
 * modes "live" | "off" | "unpaired" (the data-scanner-feed attribute).
 *
 * Delivery semantics (coordinator decision, mirrored server-side in T4):
 * AT-MOST-ONCE. The cursor advances in the same transition that emits the
 * events, and events with seq <= cursor are never emitted (the no-double-
 * emit guard — the server's consumed-marking already dedupes across
 * restarts; this guard dedupes within a session, e.g. a replayed response).
 * A tab dying mid-batch loses the batch: a lost scan is re-scannable, a
 * phantom cart line is a data error.
 *
 * POLL_INTERVAL_MS is a CLIENT-side pacing constant only — it is NEVER sent
 * over the wire (no wait_ms/hold parameter anywhere): a server-side hold
 * would freeze ui_check's virtual-time DOM dump and turn the browser gate
 * red (measured: r4rev M9). Pinned by tests/test_frontend_contract.py.
 */

const POLL_INTERVAL_MS = 1000;   // client-paced; never a request parameter

const UNPAIRED = "unpaired";
const POLLING = "polling";
const ERROR = "error";

// Pill modes (the adapter renders data-scanner-feed="<mode>").
const PILL_LIVE = "live";         // paired, polls succeeding
const PILL_OFF = "off";           // paired, server unreachable (retrying)
const PILL_UNPAIRED = "unpaired"; // no binding: the wedge path is live

function initialFeedState() {
  return { mode: UNPAIRED, cursor: 0, token: null };
}

function feedStep(state, action) {
  switch (action.type) {
    case "paired": {
      // (Re)pairing starts from cursor 0 BY DESIGN: the server's
      // consumed-marking is the cross-restart dedupe, so a fresh session
      // re-receives only what was never delivered; this cursor is
      // within-session paging.
      return {
        state: { mode: POLLING, cursor: 0, token: action.token },
        effects: [
          { type: "setPill", mode: PILL_LIVE },
          { type: "startPoll" },
        ],
      };
    }

    case "events": {
      if (state.mode === UNPAIRED || !state.token) {
        // A poll result racing an unpair: drop it (no double-emit after
        // the binding is gone).
        return { state, effects: [] };
      }
      const events = action.events || [];
      const fresh = events.filter((e) => e.seq > state.cursor);
      const effects = fresh.map((e) => ({
        type: "emitEvent", code: e.code, kind: e.kind,
      }));
      effects.push({ type: "setPill", mode: PILL_LIVE });
      // Monotonic: never regress past a cursor a later response already
      // advanced (latest_seq can drop after server-side pruning).
      const cursor = Math.max(state.cursor, action.latest_seq || 0,
        ...fresh.map((e) => e.seq), 0);
      return { state: { ...state, mode: POLLING, cursor }, effects };
    }

    case "pollError": {
      if (state.mode === UNPAIRED || !state.token) {
        return { state, effects: [] };
      }
      // The adapter's timer keeps firing — this state is "retrying", not
      // "stopped"; the pill is the only visible change.
      return {
        state: { ...state, mode: ERROR },
        effects: [{ type: "setPill", mode: PILL_OFF }],
      };
    }

    case "unpair": {
      return {
        state: initialFeedState(),
        effects: [
          { type: "stopPoll" },
          { type: "setPill", mode: PILL_UNPAIRED },
        ],
      };
    }
  }
  return { state, effects: [] };  // unknown action: inert, never a crash
}

const HcrmScanFeed = {
  POLL_INTERVAL_MS,
  UNPAIRED, POLLING, ERROR,
  PILL_LIVE, PILL_OFF, PILL_UNPAIRED,
  initialFeedState,
  feedStep,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = HcrmScanFeed;
}
if (typeof window !== "undefined") {
  window.HcrmScanFeed = HcrmScanFeed;
}
