# Task B FINAL spec — scan-capture state machine (item 1)

Design from kimi's review (r3rev-kimi.md) + qwen's §3 (r3plan-qwen.md) + glm's
adoption. The goal: a scan burst NEVER writes into any focused field; it is
intercepted, classified (QR vs barcode), and routed. Human-speed typing is
unaffected after the first character of a run.

## T0 — extract a pure reducer (`static/scancapture.js`)

Move the decision logic out of `app.js` into a new file `static/scancapture.js`
that exports (as a browser global + importable in Node) a **pure reducer**:

```
burstStep(state, {key, t}) -> {state, actions[]}
```

- States: `IDLE`, `HOLD` (accumulating a fast burst — every printable key
  prevented), `ARMED` (Enter pending, shape-gate on the buffer), `WARM` (a burst
  was replayed as human text; native for ~400 ms + snapshot, retract on
  burst-confirm).
- Constants: `SCAN_MAX_GAP_MS = 50`, `SCAN_DEFER_MS = 60`
  (`SCAN_DEFER_MS > SCAN_MAX_GAP_MS` pinned in Node AND pytest),
  `SCAN_MIN_CHARS = 4`, the badge/barcode shape gate (HCRM1: prefix or bare
  43-char token → badge; else barcode-ish → barcode; else unshaped).
- Actions emitted: `prevent`, `route(code, kind)`, `replay(buffer)`, `flush`,
  `native`. Timing source is **injected** (no test sleeps).

`index.html` loads `scancapture.js` before `app.js`; `app.js` drives the reducer
from its existing keydown listener.

## The DOM write paths (all spliced, all caret-preserving)

- A prevented burst is **never** written. On `route`, nothing is inserted. On
  abort (slow gap / non-printable / unshaped Enter / the `SCAN_DEFER_MS` flush
  timer), the buffer is **replayed** into the focused target:
  - text/textarea → `setRangeText(buffer, caret, caret, "end")` + `input` event
    (v-model-safe, caret-preserving);
  - number input → value-only splice + validate;
  - `SELECT` → `selectedIndex` + `change`;
  - `[data-scan-sink]` → **early return first** (collapsed badge field + visible
    scan field keep today's explicit path);
  - `SELECT`/`contentEditable` → pass-through (never hijacked).
- Stale-snapshot fallback: re-read the caret/selection at replay time; if the
  element is gone/changed, replay to the current caret or drop.

## Guards (each a test, not a comment)

`e.repeat` (held key = human → flush+replay), IME `isComposing`, modifier keys,
non-printable keys flush-first, `pointerdown`/`focus`/`paste` flush the pending
buffer, unshaped bursts are inert (no `onScan`, no server call). Routing is
unchanged: `onScan(code, kind)` receives exactly the same strings as today.

## Node test harness

`tests/js/scancapture.test.mjs` (run with `node --test`) drives the reducer with
scanner bursts, slow typing, fast-unshaped typing, and repeats — no DOM. Also a
pytest pin asserting `SCAN_DEFER_MS > SCAN_MAX_GAP_MS` (read from the source).
Wire `node --test` into CI's pytest job (a single `run:` step) if straightforward;
otherwise document the manual `node --test tests/js/` command.

## Invariants to hold (add as ui_check pins where visible)

1. A shape-gated burst never writes to any field.
2. Human-speed typing is native after the first char of a run.
3. The collapsed badge-code fallback still works.
4. The visible scan field's explicit path is untouched.
5. Unshaped bursts are inert.
6. No keystroke is ever lost (prevented chars are either routed or replayed).

## Gates

`node --test tests/js/`; `node --check static/app.js` + `static/scancapture.js`;
`uv run pytest`; `ui_check.py --self-host` (existing pins + the badge-field
regression pin stay green).
