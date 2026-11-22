#!/usr/bin/env bash
# HCRM portable laptop installer (P4) — one-time, idempotent, safe to re-run.
#
#   ./scripts/laptop-install.sh --server https://shop.example.com --invite CODE
#
# What it does (sudo ONLY for the udev rule; everything else is per-user):
#   1. installs the Opticon udev rule + reloads udev (re-plug afterwards);
#   2. writes ~/.config/hcrm/agent.env (0600): HCRM_SCANNER_SERVER /
#      HCRM_SCANNER_SITE_ORIGIN / HCRM_SCANNER_HEARTBEAT_SECONDS (a heartbeat
#      value already in the file is preserved across re-runs);
#   3. points ~/.config/hcrm/repo at this checkout and copies the systemd
#      USER unit verbatim into ~/.config/systemd/user/ (the unit runs the
#      daemon through the symlink, so any checkout location works);
#   4. with --invite and NO config yet: registers ONCE via scanner_daemon.py
#      --register --register-only (.venv/bin/python if present, else uv run).
#      The invite is never written to agent.env and never printed;
#   5. systemctl --user daemon-reload && enable --now the unit.
#
# Re-runs are safe: the udev/unit copies overwrite identically, the env is
# rewritten from the flags, and registration is skipped once the config
# exists (after a server-side DELETE, re-register explicitly — the skip
# note prints the exact command). --dry-run prints every command and
# changes NOTHING (the invite is redacted even in the printed command).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/laptop-install.sh --server URL [--site-origin ORIGIN]
                                 [--invite CODE] [--dry-run] [--help]

  --server URL          HCRM base URL (required), e.g. https://shop.example.com
  --site-origin ORIGIN  origin allowed to receive the pairing popup
                        (scheme://host[:port], no path; default: --server)
  --invite CODE         staff-minted single-use registration invite; without
                        it, registration is left to the daemon's first start
                        (loopback-only unless the server opts in)
  --dry-run             print every command, change nothing
  --help                this text
EOF
}

SERVER=""
SITE_ORIGIN=""
INVITE=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --server|--site-origin|--invite)
      if [ $# -lt 2 ]; then
        echo "laptop-install: $1 needs a value" >&2; exit 2
      fi
      case "$1" in
        --server) SERVER="$2" ;;
        --site-origin) SITE_ORIGIN="$2" ;;
        --invite) INVITE="$2" ;;
      esac
      shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "laptop-install: unknown flag: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$SERVER" ]; then
  echo "laptop-install: --server is required" >&2; usage >&2; exit 2
fi
case "$SERVER" in
  http://*|https://*) ;;
  *) echo "laptop-install: --server must start with http:// or https://" >&2; exit 2 ;;
esac
# The origin default tolerates a trailing slash; a path/query/fragment never
# passes (the daemon's popup guard compares scheme://host[:port] verbatim).
if [ -z "$SITE_ORIGIN" ]; then SITE_ORIGIN="${SERVER%/}"; fi
if ! printf '%s' "$SITE_ORIGIN" | grep -Eq '^https?://[A-Za-z0-9._-]+(:[0-9]+)?$'; then
  echo "laptop-install: --site-origin must be scheme://host[:port] (no path)" >&2
  exit 2
fi

# Repo root: the script lives in <root>/scripts, like run.sh. Verify the
# markers so a lone copied script refuses instead of half-installing.
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd -P)"
for marker in scripts/scanner_daemon.py scripts/udev/99-opticon-scanner.rules \
              scripts/systemd/user/hcrm-scanner-agent.service; do
  if [ ! -f "$REPO_ROOT/$marker" ]; then
    echo "laptop-install: not a HCRM repo root (missing $marker)" >&2
    exit 1
  fi
done

CONFIG_DIR="$HOME/.config/hcrm"
ENV_FILE="$CONFIG_DIR/agent.env"
CONFIG_FILE="$CONFIG_DIR/scanner.json"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hcrm-scanner-agent.service"

# Every side effect goes through run(): --dry-run prints the exact command
# (shell-quoted) and executes nothing.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

echo "laptop-install: repo $REPO_ROOT"
if [ "$DRY_RUN" = 1 ]; then
  echo "laptop-install: DRY RUN — printing commands, changing nothing"
fi

# 1. udev rule — the only privileged step. Reload applies to the NEXT plug
#    event; re-plug (or usbipd detach+attach) after installing.
run sudo cp "$REPO_ROOT/scripts/udev/99-opticon-scanner.rules" /etc/udev/rules.d/
run sudo udevadm control --reload-rules
if ! id -nG | tr ' ' '\n' | grep -qx plugdev; then
  echo "laptop-install: WARNING: $USER is not in plugdev — the udev rule grants"
  echo "  the scanner nodes to plugdev. Fix: sudo usermod -aG plugdev $USER"
  echo "  then log out and back in (the agent would see no-permission)."
fi

# 2. Per-user layout: config dir, the repo symlink the unit runs through,
#    and the unit itself (verbatim — no templating).
run mkdir -p "$CONFIG_DIR" "$UNIT_DIR"
run ln -sfn "$REPO_ROOT" "$CONFIG_DIR/repo"
run cp "$REPO_ROOT/scripts/systemd/user/$UNIT_NAME" "$UNIT_DIR/$UNIT_NAME"

# 3. agent.env (0600). The heartbeat default is 30; a value already tuned in
#    the file survives re-runs (the flags only own SERVER / SITE_ORIGIN).
HB=30
if [ -f "$ENV_FILE" ]; then
  prev="$(sed -n 's/^HCRM_SCANNER_HEARTBEAT_SECONDS=//p' "$ENV_FILE" | tail -n 1)"
  if [ -n "$prev" ]; then HB="$prev"; fi
fi
ENV_CONTENT="HCRM_SCANNER_SERVER=$SERVER
HCRM_SCANNER_SITE_ORIGIN=$SITE_ORIGIN
HCRM_SCANNER_HEARTBEAT_SECONDS=$HB
"
if [ "$DRY_RUN" = 1 ]; then
  printf 'install -m 600 /dev/stdin %q <<EOF\n%sEOF\n' "$ENV_FILE" "$ENV_CONTENT"
else
  printf '%s' "$ENV_CONTENT" | install -m 600 /dev/stdin "$ENV_FILE"
fi

# 4. Registration — once per identity. The interpreter is the repo venv when
#    present, else uv run (cwd is the repo root). An existing config means
#    the terminal is already registered: skip (a re-run must not 409), and
#    print the explicit re-register command for the after-DELETE recovery.
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY=("$REPO_ROOT/.venv/bin/python")
else
  PY=(uv run python)
fi
if [ -n "$INVITE" ]; then
  if [ -f "$CONFIG_FILE" ]; then
    echo "laptop-install: config exists — skipping registration (already registered)."
    echo "  After a server-side DELETE, re-register explicitly:"
    printf '  %q ' "${PY[@]}" scripts/scanner_daemon.py --register --register-only \
      --config "$CONFIG_FILE" --server "$SERVER" --site-origin "$SITE_ORIGIN"
    printf -- '--invite <INVITE>\n'
  elif [ "$DRY_RUN" = 1 ]; then
    # The invite is a secret: even the printed command redacts it.
    printf '%q ' "${PY[@]}" scripts/scanner_daemon.py --register --register-only \
      --config "$CONFIG_FILE" --server "$SERVER" --site-origin "$SITE_ORIGIN"
    printf -- '--invite <redacted>\n'
  else
    "${PY[@]}" scripts/scanner_daemon.py --register --register-only \
      --config "$CONFIG_FILE" --server "$SERVER" --site-origin "$SITE_ORIGIN" \
      --invite "$INVITE"
  fi
elif [ ! -f "$CONFIG_FILE" ]; then
  echo "laptop-install: note: no --invite and no config yet — the agent can"
  echo "  self-register on first start only against a loopback server; for a"
  echo "  remote server, re-run with --invite CODE (staff-minted, single-use)."
fi

# 5. The user unit: reload, then enable + start (WantedBy=default.target
#    keeps it coming back at every login).
run systemctl --user daemon-reload
run systemctl --user enable --now "$UNIT_NAME"

if [ "$DRY_RUN" = 1 ]; then
  echo "laptop-install: dry run complete — nothing was changed"
else
  echo "laptop-install: done. Re-plug the scanner, then click the 'no scanner'"
  echo "  pill in the shop browser to pair. Logs: journalctl --user -u $UNIT_NAME -f"
fi
