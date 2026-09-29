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


def check_app_route(label: str, dom: str, console: list[str], liveness: str | None,
                    copy: str) -> list[str]:
    """The compiler canary: the Vue runtime compiler must have executed."""
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
    if copy not in dom:
        failures.append(f"[{label}] expected copy {copy!r} not rendered")
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


def _check_targets(browser: str, base: str, workdir: str, report: dict) -> list[str]:
    targets = [
        ("app-root", "/", lambda dom, console, live: check_app_route(
            "app-root", dom, console, live, "Log in")),
        ("app-register", "/#/register", lambda dom, console, live: check_app_route(
            "app-register", dom, console, live, "Create your member account")),
        ("docs", "/docs", lambda dom, console, live: check_docs(dom, console, live)),
    ]
    failures: list[str] = []
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
