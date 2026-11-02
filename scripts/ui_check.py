#!/usr/bin/env python3
"""ui_check — the browser-in-the-loop gate for browser-facing behaviour.

Loads the SPA and the API docs in a real headless browser and fails on any
error console message or missing rendered element. This script exists because a
CSP change was twice shipped as "verified" with `curl -I` while it blanked the
entire UI (PLAN-v2 §0 B2/B3/B14). Four lessons are baked in, each one measured:

1. **Console messages are classified by TEXT, never by Chrome's severity.**
   The fatal regression-A `EvalError` is logged as `INFO:CONSOLE`, and a healthy
   build used to emit a benign `INFO:CONSOLE` advisory — severity filtering
   inverts the gate (B14).
2. **Records are SPLIT, never captured by one regex shape.** Chrome 131 logs
   `:CONSOLE(9)] "msg", source: url (9)`; Chrome 150 logs `:CONSOLE:9] "msg".`
   A single-shape regex silently returned **0** messages on Chrome 150, so a
   page with a live CSP violation reported "UI CHECK PASSED" (§8.2 F1). A
   capture-group regex with an optional `, source:` suffix is no better: it
   stops at the first inner double quote and turned
   `'Blocked "foo" Refused to connect'` into `'Blocked '` (§9.3).
3. **The detector proves itself before it is trusted (§9.2 F7).**
   `chrome-headless-shell` — the binary this script's own install hint used to
   recommend, and the one CI installs — emits **zero** `:CONSOLE` records under
   every flag combination. A probe page with a unique token is loaded with the
   exact flags used for the real checks; if the token does not come back
   through the parser, that browser is rejected, and if no browser passes the
   gate exits **4** ("detector blind") rather than passing.
4. **DOM assertions count OCCURRENCES, never lines:** `--dump-dom` output is
   effectively one line, so a line count of `<input` is always 1.

Usage — always against a scratch server, never a real deployment's data:

    # let the script boot and tear down its own throwaway server (preferred):
    uv run python scripts/ui_check.py --self-host

    # or point it at one you started yourself:
    HCRM_SCRATCH=1 ./scripts/run.sh &      # scratch data dir, live DB untouched
    uv run python scripts/ui_check.py --base http://127.0.0.1:8000

Exit codes: 0 pass · 1 check failure · 2 target unreachable · 3 no browser
found · 4 browser found but its console detector is blind. CI must treat 3 and
4 as failures, never as skips (PLAN-v2 §9.8).

Deliberately out of scope: CDP, scripted login, screenshots.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EXIT_PASS = 0
EXIT_CHECK_FAILED = 1
EXIT_UNREACHABLE = 2
EXIT_NO_BROWSER = 3
EXIT_DETECTOR_BLIND = 4

DEFAULT_BASE = os.environ.get("HCRM_BASE", "http://127.0.0.1:8000")

INSTALL_HELP = """No usable Chrome/Chromium found. Install one that logs console
messages to stderr — note that `chrome-headless-shell` does NOT (it emits zero
:CONSOLE records, so this gate would be blind with it):

  Debian/Ubuntu:  sudo apt-get install -y google-chrome-stable   (or: chromium)
  macOS:          https://www.google.com/chrome/
  Any platform:   npx puppeteer browsers install chrome
                  (installs full Chrome under ~/.cache/puppeteer, probed first)

Pin a specific binary with --browser or HCRM_BROWSER.
"""

# --- Console classification (lesson 1) --------------------------------------

FAIL_PATTERNS = (
    "Uncaught",
    # The i18n runtime renders "[missing: <id>]" and logs this line when a key is
    # absent from the compiled copy (item 2). It is ours, so it is a hard failure:
    # the browser gate must never certify a page that lost a string.
    "[i18n] missing message",
    "Refused to",
    "violates the following Content Security Policy",
    "Failed to load resource",
    "net::ERR_",
)

# Known-benign advisories, matched by prefix. Every entry says why it is here
# and what removes it — a shrinking allowlist is the healthy direction.
BENIGN_PREFIXES = (
    # Chrome third-party-cookie policy chatter; not ours to fix.
    "Third-party cookie",
    # Browser deprecation notices (e.g. from vendored libraries).
    "[Deprecation]",
    # REMOVED (W4.0): "[DOM] Input elements should have autocomplete attributes"
    # — every credential field now carries an autocomplete attribute, so the
    # advisory no longer appears. Do not re-add it: fix the markup instead.
)

# A console record header, in BOTH observed shapes (lesson 2):
#   [pid:tid:MMDD/HHMMSS.uuuuuu:INFO:CONSOLE(9)] "msg", source: url (9)
#   [pid:tid:MMDD/HHMMSS.uuuuuu:INFO:CONSOLE:9] "msg".
_RECORD_RE = re.compile(r"\[\d+:\d+:[^\]]*?:\w+:CONSOLE(?::|\()(\d+)\)?\]")


def console_records(stderr: str) -> list[str]:
    """Split stderr into console-record bodies.

    Splitting on record headers is immune to inner double quotes, embedded
    newlines and a missing `, source:` suffix — the three ways a capture-group
    regex silently truncates or drops a message.
    """
    hits = list(_RECORD_RE.finditer(stderr))
    bodies: list[str] = []
    for i, match in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(stderr)
        body = stderr[match.end():end].strip()
        if body.startswith('"'):
            body = body[1:]
        cut = body.rfind('", source: ')
        if cut != -1:
            body = body[:cut]
        else:
            body = re.sub(r'"\.?$', "", body.rstrip())
        bodies.append(body.rstrip())
    return bodies


def record_count(stderr: str) -> int:
    """How many console records the browser emitted (liveness denominator)."""
    return len(_RECORD_RE.findall(stderr))


def is_error(message: str) -> bool:
    if message.startswith(BENIGN_PREFIXES):
        return False
    return any(pattern in message for pattern in FAIL_PATTERNS)


def parser_liveness_failure(stderr: str) -> str | None:
    """Non-None if the browser logged console records we failed to parse.

    Complements the self-probe: the probe validates a browser once, this catches
    a parser regression on every page, at zero extra cost.
    """
    records = record_count(stderr)
    if records and not console_records(stderr):
        return (f"console parser recognised 0 of {records} record(s) — the "
                f"browser's log format changed; fix console_records()")
    return None


# --- Browser discovery + detector self-probe (lesson 3) ----------------------

_PUPPETEER_CACHE = Path.home() / ".cache" / "puppeteer"
_PATH_BROWSERS = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)
_MAC_BUNDLES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

# The exact flags used for real checks. The probe MUST use the same set: full
# Chrome 131 logs console records with --headless=new and none with
# --headless=old, so a probe run under different flags proves nothing.
BROWSER_FLAGS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--enable-logging=stderr",
    "--virtual-time-budget=6000",
    "--dump-dom",
)

PROBE_PAGE = (
    "<!doctype html><html><body><script>"
    'console.error("uicheck-detector-probe-{token} \\"inner-quoted\\"\\nsecond-line");'
    "</script>probe</body></html>\n"
)


def browser_candidates() -> list[str]:
    """Full Chrome first: chrome-headless-shell cannot log console messages."""
    found: list[str] = []
    if _PUPPETEER_CACHE.is_dir():
        for name in ("chrome", "chrome-headless-shell"):
            paths = {p for p in _PUPPETEER_CACHE.rglob(name)
                     if p.is_file() and os.access(p, os.X_OK)}
            # Newest patch level first *within* a name; never across names —
            # ordering across names is what makes the detector blind (§9.2 F7).
            found += [str(p) for p in sorted(paths, key=lambda q: q.stat().st_mtime,
                                             reverse=True)]
    found += [p for p in (shutil.which(n) for n in _PATH_BROWSERS) if p]
    found += [b for b in _MAC_BUNDLES if os.path.isfile(b)]
    unique: list[str] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def _run(browser: str, url: str, profile: str) -> tuple[str, str]:
    cmd = [browser, *BROWSER_FLAGS, f"--user-data-dir={profile}",
           "--no-first-run", "--no-default-browser-check", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return proc.stdout, proc.stderr


def probe_browser(browser: str, workdir: str) -> tuple[bool, str]:
    """True iff this binary emits console records AND our parser reads them all."""
    token = secrets.token_hex(4)
    profile = os.path.join(workdir, "probe-profile")
    page = os.path.join(workdir, f"probe-{token}.html")
    Path(page).write_text(PROBE_PAGE.format(token=token), encoding="utf-8")
    try:
        _, stderr = _run(browser, Path(page).as_uri(), profile)
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except OSError as exc:
        return False, f"probe could not run: {exc}"

    records = record_count(stderr)
    messages = console_records(stderr)
    if records == 0:
        return False, ("emitted 0 console records — this build does not log "
                       "console messages to stderr (chrome-headless-shell does not)")
    if not messages:
        return False, f"emitted {records} record(s) but the parser read none"
    if not any(token in m for m in messages):
        return False, (f"probe token missing from {len(messages)} parsed "
                       f"message(s) — messages are being dropped")
    if not any("inner-quoted" in m and "second-line" in m for m in messages):
        return False, ("probe message was truncated at an inner quote or "
                       "newline — console_records() is unsafe")
    return True, f"ok ({len(messages)}/{records} record(s) parsed)"


def choose_browser(pinned: str | None, workdir: str) -> tuple[str | None, list[dict]]:
    """Return (browser, report). Exit 4 territory when nothing passes the probe."""
    candidates = [pinned] if pinned else browser_candidates()
    report = []
    for candidate in candidates:
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            report.append({"browser": candidate, "probe": "not executable"})
            continue
        try:
            version = subprocess.run([candidate, "--version"], capture_output=True,
                                     text=True, timeout=30).stdout.strip()
        except Exception:
            version = "(version unknown)"
        ok, why = probe_browser(candidate, workdir)
        report.append({"browser": candidate, "version": version,
                       "probe": "pass" if ok else "blind", "detail": why})
        if ok:
            return candidate, report
    return None, report


# --- Page checks (lesson 4: count occurrences, never lines) ------------------

def count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


#: The copy rows each view MUST render — ids, never cells. The cells are read
#: from i18n/strings.csv for the target's language (see `_copy_cells`), so the
#: gate holds no copy of its own: a wording change cannot redden it, and a
#: language flip only changes which column is asserted.
#: `app.brand` is in both lists on purpose: the header lockup renders on EVERY
#: view, and the cell is a proper noun identical in both languages — so the
#: assertion proves the lockup is there without depending on a language.
LOGIN_COPY_IDS = (
    "app.brand", "auth.email_or_username", "auth.password", "auth.login",
    "auth.scan_anywhere", "auth.no_account",
)
REGISTER_COPY_IDS = (
    "app.brand", "auth.register_title", "auth.full_name", "auth.email",
    "auth.password_min", "auth.signup",
)


def _copy_cells(ids: tuple[str, ...], lang: str) -> list[tuple[str, str]]:
    """[(id, rendered cell)] for `lang`, from the copy source (never a literal)."""
    source = _copy_source()
    return [(rid, source[rid].cell(lang)) for rid in ids]


def _copy_source() -> dict:
    """id -> Row, read once from i18n/strings.csv via the shared loader."""
    global _COPY_SOURCE
    if _COPY_SOURCE is None:
        scripts_dir = str(Path(__file__).resolve().parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import _i18n
        rows, problems = _i18n.read_rows()
        if problems:
            raise SystemExit("i18n: " + problems[0])
        _COPY_SOURCE = {row.id: row for row in rows}
    return _COPY_SOURCE


_COPY_SOURCE: dict | None = None


def check_app_route(label: str, dom: str, console: list[str], liveness: str | None,
                    copy_ids: tuple[str, ...], expected_lang: str,
                    view: str = "") -> list[str]:
    """The compiler canary: the Vue runtime compiler must have executed.

    `copy_ids` are the rows this view must render, asserted in
    `expected_lang`'s column of i18n/strings.csv — the gate never carries copy
    itself. `view` selects the view-specific STRUCTURAL pins (login), so they
    no longer key off an English sentence.
    """
    failures = []
    if liveness:
        failures.append(f"[{label}] {liveness}")
    for message in console:
        if is_error(message):
            failures.append(f"[{label}] console error: {message!r}")
    if count(r"data-v-app", dom) < 1:
        failures.append(f"[{label}] data-v-app missing — Vue did not mount")
    if count(r"v-cloak", dom) != 0:
        failures.append(f"[{label}] v-cloak still present — template never compiled")
    if count(r"<input\b", dom) < 2:
        failures.append(f"[{label}] fewer than 2 <input> elements rendered")
    if count(r"<button\b", dom) < 1:
        failures.append(f"[{label}] no <button> rendered")
    for rid, cell in _copy_cells(copy_ids, expected_lang):
        if cell not in dom:
            failures.append(f"[{label}] {expected_lang} copy for {rid!r} not "
                            f"rendered (expected {cell!r})")
    # Task 5 pins (count()-only, per the repo's no-interaction harness):
    # - the global scan bus must be LIVE (the keydown listener's installer
    #   flips the binding that renders this attribute — a JS error in the
    #   capture code fails here even if the app still mounts);
    # - the removed badge-code field must STAY removed: the global capture
    #   ("scan anywhere") is the one browser badge path (owner decision,
    #   login-screen cleanup) — a reintroduced field would fork it.
    if count(r'data-scan-capture="on"', dom) < 1:
        failures.append(f"[{label}] data-scan-capture=\"on\" missing — "
                        "the global scan listener did not install")
    # Round 4: the scan-feed pill must render on every view (it lives in
    # the topbar) — a JS error in installScanFeed fails here even if Vue
    # still mounted, and the operator loses the "which scan path is live"
    # signal (live = daemon-forwarded, off = paired but unreachable,
    # unpaired = the wedge capture is the path). It is a BUTTON, and since
    # the one-click change it PAIRS when unpaired (no popover opens).
    if count(r'data-scanner-feed="(?:live|off|unpaired)"', dom) < 1:
        failures.append(f"[{label}] data-scanner-feed pill missing — "
                        "the round-4 scan feed did not install")
    if count(r'<button[^>]*class="[^"]*\bfeed-pill\b', dom) < 1:
        failures.append(f"[{label}] feed pill is not a button — it pairs "
                        "(unpaired) / manages the pairing (paired)")
    # One-click pairing: the manual form's inputs must never render again
    # (placeholders are what would actually show in a one-shot DOM dump).
    # ui_check cannot click, so the runtime one-click path itself is
    # covered by the contract pins + the /pair/auto router tests.
    if count(r'placeholder="counter-1"', dom) != 0 \
            or count(r'placeholder="8 characters"', dom) != 0:
        failures.append(f"[{label}] manual pairing inputs rendered — the "
                        "pill is one-click; the form must not come back")
    # Task E: auto-detect is the primary login path. The "scan anywhere" row is
    # in LOGIN_COPY_IDS (asserted above in the target's language), so its
    # PRESENCE is copy-checked; the negative "no click-to-scan wording" pin is
    # gone with the wording — a hardcoded reappearance is now the i18n
    # checker's ERROR (migrated.txt is deleted at the flip), not this gate's.
    if view == "login" and count(r"<input\b", dom) != 2:
        # Structural, not a placeholder string: the email and password fields
        # and NOTHING else. A reintroduced badge-code field would make it 3.
        failures.append(f"[{label}] the login view must render exactly the two "
                        "credential inputs (the badge-code fallback must not "
                        "come back — scan anywhere is the one badge path)")
    # The language toggle (item 3), for every app target: `expected_lang` is the
    # DEFAULT direction or the ?lang= opposite one — both polarities run.
    failures.extend(check_lang_direction(dom, expected_lang))
    return failures


_LANG_LABELS: dict[str, str] | None = None


def _lang_labels() -> dict[str, str]:
    """en/da → the toggle's accessible name, read from the copy source.

    The cell comes from i18n/strings.csv (via `_copy_source`) so a wording
    change can never redden this gate: the browser gate asserts structure and
    polarity, never copy. Lazy so importing this module (the parser tests)
    needs neither scripts/ on sys.path nor the CSV until a check runs.
    """
    global _LANG_LABELS
    if _LANG_LABELS is None:
        row = _copy_source()["lang.switch_to"]
        _LANG_LABELS = {"en": row.en, "da": row.da}
    return _LANG_LABELS


def check_lang_direction(dom: str, expected_lang: str) -> list[str]:
    """The language-toggle sentinel pair and its polarity (item 3).

    `data-lang` is the CURRENT language, `data-lang-toggle` the target (the
    flag the button draws), `data-flag` is static per SVG so a swapped
    drawing is still detectable. The accessible name is compared to the CSV
    row for the expected language — no copy is hardcoded here.
    """
    label = "lang-direction"
    target = "da" if expected_lang == "en" else "en"
    failures = []
    if count(rf'<html lang="{expected_lang}"', dom) < 1:
        failures.append(f"[{label}] <html lang> is not {expected_lang!r}")
    if count(r'data-lang-toggle="', dom) != 1:
        failures.append(f"[{label}] expected exactly one language toggle, "
                        f"found {count(r'data-lang-toggle="', dom)}")
    if count(rf'data-lang="{expected_lang}"', dom) < 1:
        failures.append(f"[{label}] data-lang must carry the current "
                        f"language ({expected_lang})")
    if count(rf'data-lang-toggle="{expected_lang}"', dom) != 0:
        failures.append(f"[{label}] inversion: data-lang-toggle must be the "
                        f"TARGET ({target}), not the current language")
    if count(rf'data-lang-toggle="{target}"', dom) < 1 \
            or count(rf'data-flag="{target}"', dom) < 1:
        failures.append(f"[{label}] the toggle must show the {target} flag "
                        f"(data-lang-toggle == data-flag == {target})")
    expected_label = _lang_labels()[expected_lang]
    if count(rf'aria-label="{re.escape(expected_label)}"', dom) < 1:
        failures.append(f"[{label}] the accessible name must be the CSV "
                        f"action row for {expected_lang}")
    return failures


def check_docs(dom: str, console: list[str], liveness: str | None) -> list[str]:
    failures = []
    if liveness:
        failures.append(f"[docs] {liveness}")
    for message in console:
        if is_error(message):
            failures.append(f"[docs] console error: {message!r}")
    if count(r"swagger-ui", dom) < 1:
        failures.append("[docs] swagger-ui container missing")
    size = len(dom.encode("utf-8", "replace"))
    if size <= 20_000:
        failures.append(f"[docs] DOM only {size} bytes (blocked/incomplete docs "
                        f"page; a rendered one is ~50 KB)")
    return failures


# --- Main --------------------------------------------------------------------

def _reachable(base: str) -> bool:
    import _hcrm
    return _hcrm.healthz(base, timeout=5.0) is not None


#: The app coverage table: (label, path, view, expected language, copy rows).
#: Danish is the DEFAULT (item 7), so "/" and "/#/register" assert the DANISH
#: column of the copy source and "/?lang=en" the English one — the cells come
#: from i18n/strings.csv, never from this file.
#:
#: The ENGLISH target runs FIRST on purpose. Each target gets its own browser
#: profile; if that isolation ever regressed, the `en` this run persists would
#: leak into the default target and its Danish pins would fail. Ordering it
#: first turns a persistence leak into a failure instead of a silent no-op.
APP_TARGETS = (
    ("app-lang-en", "/?lang=en", "login", "en", LOGIN_COPY_IDS),
    ("app-root", "/", "login", "da", LOGIN_COPY_IDS),
    ("app-register", "/#/register", "register", "da", REGISTER_COPY_IDS),
)


def _app_check(label: str, view: str, lang: str, copy_ids: tuple[str, ...]):
    """Bind one APP_TARGETS row into the (dom, console, live) callback."""
    return lambda dom, console, live: check_app_route(
        label, dom, console, live, copy_ids, lang, view)


def _check_targets(browser: str, base: str, workdir: str, report: dict) -> list[str]:
    targets = [(label, path, _app_check(label, view, lang, copy_ids))
               for label, path, view, lang, copy_ids in APP_TARGETS]
    targets.append(("docs", "/docs",
                    lambda dom, console, live: check_docs(dom, console, live)))
    failures: list[str] = []
    titles: dict[str, str] = {}
    for label, path, check in targets:
        url = base.rstrip("/") + path
        profile = os.path.join(workdir, f"profile-{label}")
        try:
            dom, stderr = _run(browser, url, profile)
        except subprocess.TimeoutExpired:
            failures.append(f"[{label}] browser timed out loading {url}")
            report["targets"][label] = {"url": url, "error": "timeout"}
            continue
        console = console_records(stderr)
        liveness = parser_liveness_failure(stderr)
        title = re.search(r"<title>(.*?)</title>", dom)
        if title:
            titles[label] = title.group(1)
        target_failures = check(dom, console, liveness)
        failures.extend(target_failures)
        report["targets"][label] = {
            "url": url,
            "dom_bytes": len(dom.encode("utf-8", "replace")),
            "console_records": record_count(stderr),
            "console": console,
            "failures": target_failures,
        }
        status = "ok" if not target_failures else "FAIL"
        print(f"  [{status:>4}] {label:<12} DOM "
              f"{report['targets'][label]['dom_bytes']:>7} B, "
              f"{record_count(stderr)} console record(s), {len(console)} parsed")
    # Both-direction proof, copy from the CSV only: the DEFAULT target ("/") and
    # the ?lang= opposite must each render THEIR language's `app.title` cell
    # (applyLang sets it before mount), the two must differ (so a stuck language
    # can never pass) and both must carry the `app.brand` proper noun.
    default = next(spec for spec in APP_TARGETS if spec[1] == "/")
    other = next(spec for spec in APP_TARGETS if spec[3] != default[3])
    for label, lang in ((default[0], default[3]), (other[0], other[3])):
        rendered = titles.get(label)
        expected = _copy_source()["app.title"].cell(lang)
        if rendered is None:
            failures.append(f"[lang-direction] {label} rendered no <title>")
        elif rendered != expected:
            failures.append(f"[lang-direction] {label} title must be the {lang} "
                            f"app.title cell {expected!r}, got {rendered!r}")
    default_title, other_title = titles.get(default[0]), titles.get(other[0])
    brand = _copy_source()["app.brand"].en      # a proper noun: one column's worth
    if default_title is not None and default_title == other_title:
        failures.append(f"[lang-direction] {default[0]} and {other[0]} must not "
                        "share a title (the language did not switch)")
    elif default_title is not None and other_title is not None \
            and (brand not in default_title or brand not in other_title):
        failures.append(f"[lang-direction] both titles must carry the brand "
                        f"{brand!r} (the app.brand row)")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Browser-in-the-loop UI verification (see module docstring).")
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"target base URL (default {DEFAULT_BASE})")
    parser.add_argument("--browser", default=os.environ.get("HCRM_BROWSER"),
                        help="browser binary to use (default: auto-detect + probe)")
    parser.add_argument("--self-host", action="store_true",
                        help="boot a throwaway server on a scratch data dir")
    parser.add_argument("--model", default=None,
                        help="HCRM_EMBED_MODEL for --self-host (default: no model, "
                             "so the boot is offline and fast)")
    parser.add_argument("--json", action="store_true",
                        help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    workdir = tempfile.mkdtemp(prefix="uicheck-")
    report: dict = {"base": None, "browser": None, "browsers": [], "targets": {}}
    try:
        import contextlib
        import _hcrm

        if args.self_host:
            stack = contextlib.ExitStack()
            model = args.model or _hcrm.NO_MODEL
            server = stack.enter_context(_hcrm.self_host(model=model))
            base = server.base
            report["self_hosted"] = {"data_dir": server.data_dir, "model": model}
        else:
            stack = contextlib.ExitStack()
            base = args.base

        with stack:
            report["base"] = base
            if not _reachable(base):
                print(f"target {base} is not reachable (/api/healthz did not "
                      f"answer). Start it, or pass --self-host.", file=sys.stderr)
                return EXIT_UNREACHABLE

            browser, browser_report = choose_browser(args.browser, workdir)
            report["browsers"] = browser_report
            for entry in browser_report:
                print(f"  browser probe: [{entry['probe']:>5}] {entry['browser']} "
                      f"{entry.get('version', '')} — {entry.get('detail', '')}")
            if browser is None:
                usable = [e for e in browser_report if e["probe"] != "not executable"]
                if not usable:
                    print(INSTALL_HELP)
                    return EXIT_NO_BROWSER
                print("\nUI CHECK INCONCLUSIVE: a browser was found but none of "
                      "them logs console messages this script can read. Refusing "
                      "to report a pass with a blind detector.", file=sys.stderr)
                return EXIT_DETECTOR_BLIND

            report["browser"] = browser
            print(f"browser: {browser}")
            print(f"target:  {base}")
            failures = _check_targets(browser, base, workdir, report)

            if args.json:
                print(json.dumps(report, indent=2))
            if failures:
                print("\nUI CHECK FAILED:")
                for failure in failures:
                    print(f"  - {failure}")
                return EXIT_CHECK_FAILED
            print("\nUI CHECK PASSED")
            return EXIT_PASS
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
