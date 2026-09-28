#!/usr/bin/env python3
"""ui_check — the browser-in-the-loop gate for browser-facing behaviour.

Loads the SPA and the API docs in a real headless Chrome and fails on any
error console message or missing rendered element. This script exists
because a CSP change was twice shipped as "verified" with `curl -I` while it
blanked the entire UI (PLAN-v2 §0, B2/B3/B14). Two lessons are baked in:

1. Console messages are classified by TEXT, never by Chrome's severity
   field: fatal errors (e.g. `Uncaught EvalError: ... 'unsafe-eval' ...`)
   are logged as `INFO:CONSOLE`, and the healthy build emits a benign
   `INFO:CONSOLE` advisory — severity-based filtering inverts the gate.
2. DOM assertions count OCCURRENCES (re.findall), never lines: `--dump-dom`
   output is effectively one line, so a line count of `<input` is always 1.

Usage — always against a scratch server, never a real deployment's data:

    HCRM_DATA_DIR=$(mktemp -d) HCRM_EMBED_MODEL=bogus/model \\
        uv run uvicorn app.main:app --port 8000 &
    uv run python scripts/ui_check.py [--base http://127.0.0.1:8000] [--json]

Exit codes: 0 = pass · 1 = check failure · 3 = no browser found.
Deliberately out of scope: CDP, scripted login, screenshots.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE = os.environ.get("HCRM_BASE", "http://127.0.0.1:8000")

INSTALL_HELP = """No Chrome/Chromium browser found. Install one, e.g.:

  Debian/Ubuntu:  sudo apt-get install -y chromium-browser
  macOS:          https://www.google.com/chrome/
  Any platform:   npx puppeteer browsers install chrome-headless-shell
                  (installs under ~/.cache/puppeteer, which is probed first)
"""

# --- Console classification (B14/N3: severity is inverted; match the text) ---

FAIL_PATTERNS = (
    "Uncaught",
    "Refused to",
    "violates the following Content Security Policy",
    "Failed to load resource",
    "net::ERR_",
)

# Known-benign advisories: allowed by prefix. Every entry keeps the reason it
# is here and what would let it be removed — a shrinking allowlist is the
# healthy direction (W6.2 removes the first entry by adding autocomplete
# attributes to the auth forms, at which point DELETE IT).
BENIGN_PREFIXES = (
    # Chrome DOM hint on the login/register inputs; W6.2 (autocomplete
    # attributes) makes it disappear — remove this entry then.
    "[DOM] Input elements should have autocomplete attributes",
    # Chrome third-party-cookie policy chatter; not ours to fix.
    "Third-party cookie",
    # Browser deprecation notices (e.g. from vendored libraries).
    "[Deprecation]",
)

# `...:INFO:CONSOLE(0)] "message text", source: http://... (line)`. Chrome
# does NOT escape the message: it can contain raw quotes AND newlines (the
# regression-A EvalError message ends with a newline inside the quotes), so
# this must be matched against the WHOLE stderr with DOTALL — line-based
# parsing silently drops multi-line messages. The closing quote is the one
# followed by ", source: " (inner quotes never are).
_CONSOLE_RE = re.compile(
    r':CONSOLE\(\d+\)\]\s*"(.*?)"(?:, source: [^\n]*)', re.DOTALL
)


def _console_messages(stderr: str) -> list[str]:
    return [m.group(1).rstrip("\n") for m in _CONSOLE_RE.finditer(stderr)]


def _is_error(msg: str) -> bool:
    if msg.startswith(BENIGN_PREFIXES):
        return False
    return any(p in msg for p in FAIL_PATTERNS)


# --- Browser discovery ------------------------------------------------------

_PUPPETEER_CACHE = Path.home() / ".cache" / "puppeteer"
_PATH_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
_MAC_BUNDLES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def _from_puppeteer_cache() -> list[Path]:
    """chrome-headless-shell first (smallest, CI-friendly), then full Chrome."""
    if not _PUPPETEER_CACHE.is_dir():
        return []
    found: list[Path] = []
    for name in ("chrome-headless-shell", "chrome"):
        for p in _PUPPETEER_CACHE.rglob(name):
            if p.is_file() and os.access(p, os.X_OK):
                found.append(p)
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


def find_browser() -> str | None:
    for p in _from_puppeteer_cache():
        return str(p)
    for name in _PATH_BROWSERS:
        if shutil.which(name):
            return shutil.which(name)
    for bundle in _MAC_BUNDLES:
        if os.path.isfile(bundle):
            return bundle
    return None


# --- Page runner ------------------------------------------------------------

def run_page(browser: str, url: str) -> tuple[str, list[str]]:
    """Load `url` headlessly; return (dom, console_messages)."""
    cmd = [
        browser,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--enable-logging=stderr",
        "--virtual-time-budget=6000",
        "--dump-dom",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc.stdout, _console_messages(proc.stderr)


# --- Checks -----------------------------------------------------------------

def count(pattern: str, text: str) -> int:
    """R1: assertions count occurrences, never lines (dump-dom is 1 line)."""
    return len(re.findall(pattern, text))


def check_app_route(label: str, dom: str, console: list[str], copy: str) -> list[str]:
    """The compiler canary: the Vue runtime compiler must have executed."""
    failures = []
    for msg in console:
        if _is_error(msg):
            failures.append(f"[{label}] console error: {msg!r}")
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


def check_docs(dom: str, console: list[str]) -> list[str]:
    failures = []
    for msg in console:
        if _is_error(msg):
            failures.append(f"[docs] console error: {msg!r}")
    if count(r"swagger-ui", dom) < 1:
        failures.append("[docs] swagger-ui container missing")
    if len(dom.encode("utf-8", "replace")) <= 20_000:
        failures.append(
            f"[docs] DOM only {len(dom.encode('utf-8', 'replace'))} bytes "
            "(blocked/incomplete docs page; a rendered one is ~50 KB)"
        )
    return failures


# --- Main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Browser-in-the-loop UI verification (see module docstring)."
    )
    parser.add_argument(
        "--base", default=DEFAULT_BASE, help=f"target base URL (default {DEFAULT_BASE})"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable summary"
    )
    args = parser.parse_args()

    browser = find_browser()
    if browser is None:
        print(INSTALL_HELP)
        return 3

    base = args.base.rstrip("/")
    try:
        version = subprocess.run(
            [browser, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except Exception:
        version = "(version unknown)"
    print(f"browser: {browser} ({version})")
    print(f"target:  {base}")

    targets = [
        ("app-root", "/", lambda dom, console: check_app_route(
            "app-root", dom, console, "Log in")),
        ("app-register", "/#/register", lambda dom, console: check_app_route(
            "app-register", dom, console, "Create your member account")),
        ("docs", "/docs", lambda dom, console: check_docs(dom, console)),
    ]

    failures: list[str] = []
    report = {"browser": browser, "base": base, "targets": {}}
    for label, path, check in targets:
        url = base + path
        try:
            dom, console = run_page(browser, url)
        except subprocess.TimeoutExpired:
            failures.append(f"[{label}] browser timed out loading {url}")
            report["targets"][label] = {"error": "timeout"}
            continue
        target_failures = check(dom, console)
        failures.extend(target_failures)
        report["targets"][label] = {
            "url": url,
            "dom_bytes": len(dom.encode("utf-8", "replace")),
            "console": console,
            "failures": target_failures,
        }
        size = report["targets"][label]["dom_bytes"]
        status = "ok" if not target_failures else "FAIL"
        print(f"  [{status:>4}] {label:<12} DOM {size:>7} B, "
              f"{len(console)} console message(s)")

    if args.json:
        print(json.dumps(report, indent=2))

    if failures:
        print("\nUI CHECK FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nUI CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
