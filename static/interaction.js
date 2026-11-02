/* HCRM interaction helpers — the pure, node-testable half of the device controls.
 *
 * Two behaviours live here:
 *   C2 — the device font-size stepper (grid, clamp, storage, the exact root px);
 *   C3 — the hold-to-confirm state machine for the final purchase button and the
 *        keyed double-tap guard for money/record-changing actions.
 * The split is the scancapture.js one: this file decides (timing, phases,
 * exactly-once, per-key windows) and `app.js` owns the Vue state and the
 * pointer/keyboard/DOM wiring, so `node --test tests/js/holdguard.test.mjs` can
 * drive every boundary without a browser. The only DOM touch is `applyScale`,
 * which takes the document as an argument (the `applyLang` idiom) instead of
 * reaching for a global.
 *
 * The font scale is a DEVICE preference, like `hcrm_lang` and the terminal
 * token: stored in localStorage["hcrm_font_scale"], never written on a first
 * visit (only a button press reaches the writer) and never cleared on logout.
 * Storage access throws on a locked-down kiosk (and in Safari private mode), so
 * every read/write is guarded: a failed READ is the default, a failed WRITE is
 * silent, and neither can crash the boot or a button press.
 *
 * Grid: 1 (100 %) is the default and the stepping anchor. FONT_SCALE_MIN and
 * FONT_SCALE_MAX are the END STOPS, not grid points: a step down from 0.9
 * lands on 0.85 and a step up from 1.3 lands on 1.35. Stepping back from an
 * end returns to the nearest grid point (1.35 -> 1.3, 0.85 -> 0.9), so the UI
 * never oscillates between an off-grid 1.25 and the 1.35 ceiling, and 100 %
 * stays reachable. This is why 1 is on the grid even though the bounds are
 * 0.85/1.35 - every ordinary step is exactly 0.1, and each end is one clamp
 * away.
 *
 * Dual classic-script/CommonJS export, exactly the scancapture / i18n idiom.
 * index.html loads this BEFORE app.js (pinned in tests/test_frontend_contract.py).
 */
(function (global) {
  const HOLD_MS = 700;        // the final purchase button's hold threshold
  const GUARD_MS = 600;       // a same-key re-activation inside this window is ignored

  /** Monotonic milliseconds; Date.now is the fallback for an old webview. */
  function defaultNow() {
    return typeof performance !== "undefined" && typeof performance.now === "function"
      ? performance.now() : Date.now();
  }

  function defaultRequestFrame(callback) {
    if (typeof requestAnimationFrame === "function") return requestAnimationFrame(callback);
    return setTimeout(() => callback(defaultNow()), 16);
  }

  function defaultCancelFrame(id) {
    if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(id);
    else clearTimeout(id);
  }

  function positive(value, fallback) {
    return Number.isFinite(value) && value > 0 ? value : fallback;
  }

  const FONT_SCALE_KEY = "hcrm_font_scale";
  const FONT_SCALE_MIN = 0.85;
  const FONT_SCALE_MAX = 1.35;
  const FONT_SCALE_STEP = 0.1;
  const FONT_SCALE_DEFAULT = 1;
  const ROOT_FONT_PX = 16;   // the browser default the scale multiplies

  /** Two decimals: kills the binary-float drift of repeated 0.1 steps
   *  (1.1 + 0.1 = 1.2000000000000002) before a value is shown or stored. */
  function round2(value) {
    return Math.round(value * 100) / 100;
  }

  /**
   * A stored/asked-for scale, or null when it must be IGNORED.
   *
   * Accepts a finite number or a numeric string inside [0.85, 1.35]; rejects
   * everything else — null/undefined, blanks, non-numeric text, booleans and
   * objects (a bare Number() would turn true into 1), NaN/Infinity and values
   * outside the range. In-range values are rounded to 2 dp but NOT snapped to
   * the 0.1 grid: the spec says only non-numeric or out-of-range values are
   * ignored, and a device that once stored 1.03 keeps working.
   */
  function normaliseScale(raw) {
    if (typeof raw !== "number" && typeof raw !== "string") return null;
    if (typeof raw === "string" && raw.trim() === "") return null;
    const value = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(value)) return null;
    if (value < FONT_SCALE_MIN || value > FONT_SCALE_MAX) return null;
    return round2(value);
  }

  /**
   * readScale(storage?) -> number. The stored preference, else the default.
   * NEVER writes (a first visit must stay unwritten) and never throws: a
   * storage-blocked device simply boots at 100 %.
   */
  function readScale(storage) {
    try {
      const store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
      const stored = store && store.getItem(FONT_SCALE_KEY);
      const scale = normaliseScale(stored);
      if (scale !== null) return scale;
    } catch (_) {
      /* storage unavailable: fall through to the default */
    }
    return FONT_SCALE_DEFAULT;
  }

  /**
   * writeScale(storage?, scale) -> boolean. Persists the normalised value;
   * returns false (and writes nothing) for junk or a storage that throws, so
   * an unusable preference is never stored and a button press still works.
   */
  function writeScale(storage, scale) {
    const value = normaliseScale(scale);
    if (value === null) return false;
    try {
      const store = storage || (typeof localStorage !== "undefined" ? localStorage : null);
      if (!store) return false;
      store.setItem(FONT_SCALE_KEY, String(value));
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * stepScale(scale, direction) -> number. One step in `direction`'s sign: the
   * next value on the 1-anchored 0.1 grid, or the clamp bound when that grid
   * point would fall outside the range. `direction` 0/non-finite keeps the
   * current (normalised) value, so a stray call can never invent a size. A junk
   * base starts from the default — the same fallback every other entry point
   * uses. Integer hundredths keep repeated 0.1 steps drift-free.
   */
  function stepScale(scale, direction) {
    const current = normaliseScale(scale);
    const base = current === null ? FONT_SCALE_DEFAULT : current;
    const dir = Number(direction);
    if (!Number.isFinite(dir) || dir === 0) return base;
    const anchor = Math.round(FONT_SCALE_DEFAULT * 100);   // 100 hundredths
    const stride = Math.round(FONT_SCALE_STEP * 100);      // 10 hundredths
    const offset = Math.round(base * 100) - anchor;
    // The next GRID point strictly past the current value: floor+1 up,
    // ceil-1 down. An off-grid end stop (1.35/0.85) therefore steps back onto
    // the nearest grid point rather than onto a second off-grid value.
    const index = dir > 0
      ? Math.floor(offset / stride) + 1
      : Math.ceil(offset / stride) - 1;
    const next = (anchor + index * stride) / 100;
    return round2(Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, next)));
  }

  /** The root font size in px for a scale (16 * scale, two decimals). */
  function rootPx(scale) {
    const value = normaliseScale(scale);
    return round2(ROOT_FONT_PX * (value === null ? FONT_SCALE_DEFAULT : value));
  }

  /**
   * applyScale(scale, doc?) -> the applied scale. The stylesheet is rem-based,
   * so this ONE root value scales the whole UI. Idempotent; a missing document
   * (node) is not an error. Junk applies the default rather than leaving a
   * previous size on screen (the boot read can only pass a parsed value, but a
   * watcher is a second entry point and must not be able to strand the UI).
   */
  function applyScale(scale, doc) {
    const value = normaliseScale(scale);
    const resolved = value === null ? FONT_SCALE_DEFAULT : value;
    const target = doc || (typeof document !== "undefined" ? document : null);
    if (target && target.documentElement && target.documentElement.style) {
      target.documentElement.style.fontSize = rootPx(resolved) + "px";
    }
    return resolved;
  }

  /**
   * createHold(options) -> the hold-to-confirm state machine.
   *
   * Phases: "idle" -> "holding" (pointerdown / keydown, one 700 ms run) ->
   * "latched" (the threshold was reached and the action fired; the run stays
   * latched until the input is released, so key auto-repeat and a second
   * pointerdown cannot start another run) -> "idle".
   *
   * Events:
   *   start(source)      idle only; returns false while holding/latched.
   *   release(source, reason)  holding: fires iff elapsed >= duration, else
   *                      cancels with `reason`; latched: resets SILENTLY (the
   *                      action already ran); a source mismatch is ignored, so
   *                      a stray pointerup cannot end a keyboard hold (and
   *                      vice versa).
   *   cancel(reason)     holding: cancels + onCancel(reason); latched: silent
   *                      reset; idle: ignored. Escape/blur route here.
   *   tick(t?)           one rAF step; emits progress and fires at threshold.
   *                      Exposed so node tests can drive the clock directly.
   *
   * Callbacks: onProgress(0..1) every step (the adapter writes --hold),
   * onState(phase) on each transition, onFire() EXACTLY ONCE per completed
   * run, onCancel(reason) once per cancelled run. A completed or cancelled run
   * resets to idle; a new gesture is a new run (the 600 ms guard is the
   * separate, additional anti-double-activation layer).
   *
   * The clock, the scheduler and all four callbacks are injectable; the
   * defaults are performance.now / requestAnimationFrame. `duration` defaults
   * to 700.
   */
  function createHold(options = {}) {
    const duration = positive(options.duration, HOLD_MS);
    const now = typeof options.now === "function" ? options.now : defaultNow;
    const requestFrame = typeof options.requestFrame === "function"
      ? options.requestFrame : defaultRequestFrame;
    const cancelFrame = typeof options.cancelFrame === "function"
      ? options.cancelFrame : defaultCancelFrame;
    const onProgress = typeof options.onProgress === "function" ? options.onProgress : () => {};
    const onState = typeof options.onState === "function" ? options.onState : () => {};
    const onFire = typeof options.onFire === "function" ? options.onFire : () => {};
    const onCancel = typeof options.onCancel === "function" ? options.onCancel : () => {};

    let phase = "idle";      // idle | holding | latched
    let source = null;
    let startedAt = 0;
    let frameId = null;
    let progress = 0;

    function emit(value) {
      const p = Number.isFinite(value) ? value : 0;
      progress = Math.max(0, Math.min(1, p));
      onProgress(progress);
    }

    function setPhase(next) {
      if (phase === next) return;
      phase = next;
      onState(phase);
    }

    function clearFrame() {
      if (frameId !== null) {
        cancelFrame(frameId);
        frameId = null;
      }
    }

    function schedule() {
      if (frameId !== null) return;
      frameId = requestFrame(frame);
    }

    // Reaching the threshold: emit the full bar, latch (so nothing can fire
    // this run twice), then run the action. The state is latched BEFORE the
    // callback, so a re-entrant start() from the action is ignored.
    function finish() {
      clearFrame();
      emit(1);
      setPhase("latched");
      onFire();
    }

    function reset() {
      clearFrame();
      emit(0);
      source = null;
      setPhase("idle");
    }

    function tick(at) {
      if (phase !== "holding") return progress;
      const t = typeof at === "number" && Number.isFinite(at) ? at : now();
      const elapsed = Math.max(0, t - startedAt);
      if (elapsed >= duration) finish();
      else emit(elapsed / duration);
      return progress;
    }

    function frame() {
      frameId = null;
      if (phase !== "holding") return;
      tick();
      if (phase === "holding") schedule();
    }

    function start(from) {
      if (phase !== "idle") return false;
      source = typeof from === "string" && from ? from : "pointer";
      startedAt = now();
      emit(0);
      setPhase("holding");
      schedule();
      return true;
    }

    function release(from, reason) {
      if (phase === "idle") return false;
      if (from && source && from !== source) return false;
      if (phase === "latched") {
        reset();
        return true;
      }
      const elapsed = Math.max(0, now() - startedAt);
      if (elapsed >= duration) {
        // The threshold passed but the frame was throttled/not delivered: the
        // gesture still completed. Fire, then end the run (no latch needed —
        // the release is its end).
        finish();
        reset();
        return true;
      }
      clearFrame();
      emit(0);
      source = null;
      setPhase("idle");
      onCancel(reason || from || "release");
      return true;
    }

    function cancel(reason) {
      if (phase === "idle") return false;
      if (phase === "latched") {
        // The action already ran: never show a "cancelled" hint after commit.
        reset();
        return true;
      }
      clearFrame();
      emit(0);
      source = null;
      setPhase("idle");
      onCancel(reason || "cancel");
      return true;
    }

    return {
      start, release, cancel, tick,
      active: () => phase !== "idle",
      progress: () => progress,
      phase: () => phase,
    };
  }

  /**
   * createGuard(options) -> { guarded(key, fn, ms?) }.
   *
   * The double-tap guard: a second activation of the SAME key is ignored while
   * the first call is in flight, and for `ms` after it started (default 600).
   * Keys are independent, so two different actions never cross-block, and a
   * call after the window is allowed again. A sync/async return value passes
   * through; a thrown error clears the in-flight flag (and still propagates).
   *
   * The window is kept after a fast call settles: the point is to ignore the
   * second tap of a double-tap, which usually arrives after a sync handler
   * already returned. A slow (async) first call blocks until it settles even
   * past the window — no second request can race the first.
   */
  function createGuard(options = {}) {
    const defaultMs = Number.isFinite(options.ms) && options.ms >= 0 ? options.ms : GUARD_MS;
    const now = typeof options.now === "function" ? options.now : defaultNow;
    const entries = new Map();

    function guarded(key, fn, ms) {
      const id = String(key);
      const windowMs = ms === undefined ? defaultMs
        : (Number.isFinite(ms) && ms >= 0 ? ms : defaultMs);
      const t = now();
      const entry = entries.get(id);
      if (entry && (entry.inFlight || t < entry.readyAt)) return undefined;
      const next = { inFlight: true, readyAt: t + windowMs };
      entries.set(id, next);
      let result;
      try {
        result = fn();
      } catch (err) {
        next.inFlight = false;
        throw err;
      }
      if (result && typeof result.then === "function") {
        const settle = () => { next.inFlight = false; };
        result.then(settle, settle);
      } else {
        next.inFlight = false;
      }
      return result;
    }

    return { guarded };
  }

  const HcrmInteraction = {
    FONT_SCALE_KEY, FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_STEP,
    FONT_SCALE_DEFAULT, ROOT_FONT_PX, HOLD_MS, GUARD_MS,
    normaliseScale, readScale, writeScale, stepScale, rootPx, applyScale,
    createHold, createGuard,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = HcrmInteraction;
  global.HcrmInteraction = HcrmInteraction;
})(typeof window !== "undefined" ? window : globalThis);
