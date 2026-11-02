/* HCRM interaction helpers — the pure, node-testable half of the device controls.
 *
 * Design refresh C2 (adjustable font size) lives here first; C3's
 * hold-to-confirm state machine and keyed double-tap guard will join it. The
 * split is the scancapture.js one: this file decides (grid, clamp, storage,
 * the exact root px) and `app.js` owns the Vue state and the button wiring, so
 * `node --test tests/js/fontscale.test.mjs` can drive every boundary without a
 * DOM. The only DOM touch is `applyScale`, which takes the document as an
 * argument (the `applyLang` idiom) instead of reaching for a global.
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
 * index.html loads this BEFORE app.js (pinned in tests/test_font_scale.py).
 */
(function (global) {
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

  const HcrmInteraction = {
    FONT_SCALE_KEY, FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_STEP,
    FONT_SCALE_DEFAULT, ROOT_FONT_PX,
    normaliseScale, readScale, writeScale, stepScale, rootPx, applyScale,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = HcrmInteraction;
  global.HcrmInteraction = HcrmInteraction;
})(typeof window !== "undefined" ? window : globalThis);
