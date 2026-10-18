# Round 4 T6 FINAL spec — SPA scan-feed

The SPA consumes the daemon-forwarded queue and feeds it into the existing scan
bus. The round-3 keyboard-wedge capture stays as the **fallback** (when no
daemon is paired, the scanner reverts to wedge and the browser capture handles
focused scans).

## `static/scanfeed.js` (pure reducer, Node-importable)

`feedStep(state, action) -> {state, effects[]}` over states
`unpaired | polling | error`, holding `{cursor, token}`. Actions: `paired(token)`,
`events(list)`, `pollError`, `unpair`. Effects: `startPoll`, `stopPoll`,
`emitEvent(code, kind)`, `setPill(mode)`. `POLL_INTERVAL_MS = 1000`. No DOM, no
timers (injected clock).

## `static/app.js` adapter

- On boot: read `localStorage["hcrm_terminal_token"]`; if present →
  `feedStep({cursor:0, token}, {type:"paired"})` and start the paced short poll.
- Poll: `GET /api/scanner/events?after=<cursor>` with the terminal token
  (the `api(path, {token, raw})` foreign-token form — the token is NOT a user
  bearer token). On 200: advance cursor to `latest_seq`, then for each
  `{code, kind}` call the existing `enqueueScan(code, kind)` (which routes
  badge/barcode through the same bus as the wedge). On 401 → `unpair` + pill
  "unpaired". On network error → pill "off", keep retrying.
- **No double-handling**: while the daemon holds the grab, the browser receives
  no wedge keystrokes (so the two paths are mutually exclusive by construction).
- Status pill in the header: `data-scanner-feed="live" | "unpaired" | "off"`.

## Pairing panel (login screen)

A small "Pair this terminal" affordance: an 8-char code field →
`POST /api/scanner/pair {terminal_id, code}` (terminal_id is the daemon's; ask
the user for it alongside the code, or read from a single input "terminal:code")
→ store the returned token in localStorage → `paired(token)`. Clear the token on
logout is NOT required (the terminal binding survives users; it is per-kiosk).

## Tests

- `tests/js/scanfeed.test.mjs` (`node --test`): reducer transitions (unpaired →
  paired → polling → error → unpair), cursor advance, effect emission, no
  double-emit.
- frontend-contract pins (pytest): script order (scanfeed.js before app.js),
  the wedge capture is NOT removed, and `POLL_INTERVAL_MS` is not sent over the
  wire (no `wait_ms` param) — the no-hold ui_check rule.

## Gates

`node --test tests/js/*.test.mjs`; `node --check static/app.js static/scanfeed.js`;
`uv run pytest`; `ui_check.py --self-host` (existing pins green, zero console).
