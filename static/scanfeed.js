/* HCRM scan feed — pure poll-channel + popup state machine.
 *
 * All DOM work, timers, popup navigation, API calls, and localStorage live in
 * app.js. This file only computes state and the adapter's next effects.
 *
 * States (mode): UNPAIRED (no token) · POLLING · ERROR (poll failed, retrying).
 * data-scanner-feed stays live|off|unpaired. detail is null or no-agent |
 * no-device | no-permission | bound-elsewhere | register-denied | server-down |
 * popup-blocked. A successful poll with a present agent is live only for
 * online + device_status=present; a missing agent field is the legacy rule
 * (successful poll => live), never an inferred offline state.
 *
 * Actions: popup{reclaim} · offer{nonce,terminal_id,code} · pairFailed{reason}
 * · reclaim · paired{token} · events{events,latest_seq,agent?} · pollError ·
 * unpair. Effects: openPopup · closePopup · armWatchdog · clearWatchdog ·
 * pairRequest · setDetail · startPoll · stopPoll · emitEvent · setPill.
 * Reclaim is deliberately capped at one attempt per popup session.
 *
 * Delivery stays at-most-once: the cursor advances with emitted events and
 * seq <= cursor is never emitted twice. POLL_INTERVAL_MS is a client constant,
 * never a request parameter.
 */

const POLL_INTERVAL_MS = 1000;

const UNPAIRED = "unpaired";
const POLLING = "polling";
const ERROR = "error";

const PILL_LIVE = "live";
const PILL_OFF = "off";
const PILL_UNPAIRED = "unpaired";

const DETAILS = Object.freeze([
  "no-agent", "no-device", "no-permission", "bound-elsewhere",
  "register-denied", "server-down", "popup-blocked",
]);
const DETAIL_SET = new Set(DETAILS);

function initialFeedState() {
  return { mode: UNPAIRED, cursor: 0, token: null, detail: null,
           reclaimUsed: false };
}

function failure(state, reason) {
  const mode = state.token ? state.mode : UNPAIRED;
  return {
    state: { ...state, mode, detail: reason },
    effects: [
      { type: "setDetail", reason },
      { type: "clearWatchdog" },
      { type: "closePopup" },
      { type: "setPill", mode: state.token ? PILL_OFF : PILL_UNPAIRED },
    ],
  };
}

function feedStep(state, action) {
  switch (action.type) {
    case "popup":
      // A new popup session resets the one-reclaim budget, never the cursor.
      return {
        state: { ...state, mode: UNPAIRED, detail: null, reclaimUsed: false },
        effects: [
          { type: "openPopup", reclaim: action.reclaim },
          { type: "armWatchdog" },
        ],
      };

    case "offer":
      // The adapter validates origin/source/type/nonce/schema before this
      // action; the reducer never turns that into a token without /pair.
      return {
        state,
        effects: [
          { type: "pairRequest", terminal_id: action.terminal_id,
            code: action.code },
          { type: "clearWatchdog" },
        ],
      };

    case "pairFailed":
      if (!DETAIL_SET.has(action.reason)) return { state, effects: [] };
      return failure(state, action.reason);

    case "reclaim":
      if (state.reclaimUsed) return failure(state, "bound-elsewhere");
      return {
        state: { ...state, reclaimUsed: true, detail: null },
        effects: [
          { type: "openPopup", reclaim: 1 },
          { type: "armWatchdog" },
        ],
      };

    case "paired":
      // Token issuance is not streaming proof. The first successful poll
      // chooses live/off; do not repeat the old immediate-live optimism.
      return {
        state: { mode: POLLING, cursor: 0, token: action.token,
                 detail: null, reclaimUsed: false },
        effects: [{ type: "startPoll" }],
      };

    case "events": {
      if (state.mode === UNPAIRED || !state.token) {
        return { state, effects: [] };
      }
      const events = action.events || [];
      const fresh = events.filter((e) => e.seq > state.cursor);
      const effects = fresh.map((e) => ({
        type: "emitEvent", code: e.code, kind: e.kind,
      }));
      let detail = null;
      let healthy = true;
      if (action.agent !== undefined) {
        const agent = action.agent;
        healthy = Boolean(agent && agent.online
          && agent.device_status === "present");
        if (!healthy) {
          if (!agent || !agent.online) detail = "no-agent";
          else if (agent.device_status === "no-permission") {
            detail = "no-permission";
          } else {
            detail = "no-device";
          }
        }
      }
      // Undefined agent = old server/daemon: exactly today's successful-poll
      // behavior. A present object has to prove both freshness and presence.
      effects.push({ type: "setPill", mode: healthy ? PILL_LIVE : PILL_OFF });
      effects.push({ type: "setDetail", reason: detail });
      const cursor = Math.max(state.cursor, action.latest_seq || 0,
        ...fresh.map((e) => e.seq), 0);
      return { state: { ...state, mode: POLLING, cursor, detail }, effects };
    }

    case "pollError":
      if (state.mode === UNPAIRED || !state.token) {
        return { state, effects: [] };
      }
      // The timer keeps running: off/server-down is a retry state, not a stop.
      return {
        state: { ...state, mode: ERROR, detail: "server-down" },
        effects: [
          { type: "setPill", mode: PILL_OFF },
          { type: "setDetail", reason: "server-down" },
        ],
      };

    case "unpair":
      return {
        state: initialFeedState(),
        effects: [
          { type: "stopPoll" },
          { type: "setPill", mode: PILL_UNPAIRED },
          { type: "setDetail", reason: null },
        ],
      };
  }
  return { state, effects: [] };
}

const HcrmScanFeed = {
  POLL_INTERVAL_MS,
  UNPAIRED, POLLING, ERROR,
  PILL_LIVE, PILL_OFF, PILL_UNPAIRED,
  DETAILS,
  initialFeedState,
  feedStep,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = HcrmScanFeed;
}
if (typeof window !== "undefined") {
  window.HcrmScanFeed = HcrmScanFeed;
}
