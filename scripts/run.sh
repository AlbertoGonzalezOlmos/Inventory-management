#!/usr/bin/env bash
# Start the HCRM server (installs dependencies on first run).
# Everything runs embedded: SQLite + the sqlite-vector extension — no
# external database server needed.
#
# Binds to localhost by default. To expose the server on the network:
#   HCRM_HOST=0.0.0.0 ./scripts/run.sh
#
# HCRM_SCRATCH=1 boots on a throwaway database instead of ./data — use it for
# any experiment, verification run or browser check (PLAN-v2 ground rule 3:
# nothing runs against ./data by accident).
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${HCRM_HOST:-127.0.0.1}"
PORT="${HCRM_PORT:-8000}"

# W1.4 + strict parse: HCRM_SCRATCH decides which DATABASE gets opened — a
# data-safety decision, so it is settled FIRST, before anything is spawned
# or synced (a refused boot must cost nothing). Any non-empty value used to
# mean "on", so HCRM_SCRATCH=0 silently booted a throwaway DB (the likeliest
# way the operator ever saw an empty catalogue). Unrecognised values refuse
# the boot rather than guess (the PLAN-v2 §8.2 F5 class of accident).
SCRATCH="${HCRM_SCRATCH:-0}"
case "$SCRATCH" in
    1|true|TRUE|yes|YES|on|ON) SCRATCH=1 ;;
    0|""|false|FALSE|no|NO|off|OFF) SCRATCH=0 ;;
    *)
        echo "refusing to boot: HCRM_SCRATCH=$HCRM_SCRATCH is not one of 1/0/true/false/yes/no/on/off" >&2
        exit 1 ;;
esac
export HCRM_SCRATCH="$SCRATCH"     # normalised for the children
if [ "$SCRATCH" = 1 ]; then
    HCRM_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hcrm-scratch-XXXXXX")"
    export HCRM_DATA_DIR
fi

# W4.0 guard: the dev admin credential (admin/admin, HCRM_DEV_ADMIN, on by
# default this round) must never be reachable from the network by accident.
DEV_ADMIN="${HCRM_DEV_ADMIN:-1}"
case "$DEV_ADMIN" in 0|false|FALSE|no|off) DEV_ADMIN="" ;; esac
case "$HOST" in ""|127.0.0.1|localhost|::1) LOOPBACK=1 ;; *) LOOPBACK="" ;; esac
if [ -n "$DEV_ADMIN" ] && [ -z "$LOOPBACK" ] && [ -z "${HCRM_ALLOW_INSECURE_BIND:-}" ]; then
    echo "refusing to bind $HOST: dev admin mode is ON, so username 'admin'" >&2
    echo "with password 'admin' would have full admin access on your network." >&2
    echo "Either keep the loopback bind, set HCRM_DEV_ADMIN=0, or override with" >&2
    echo "HCRM_ALLOW_INSECURE_BIND=1 (trusted network only)." >&2
    exit 1
fi

uv sync

# i18n: the served copy is compiled from i18n/strings.csv. Refuse to boot on a
# stale artifact — a kiosk must never serve copy nobody reviewed, and must never
# rewrite its own served assets. Rebuilding is a human command (see i18n/README.md).
if ! uv run python scripts/build_i18n.py --check; then
    echo "i18n: refusing to boot — static/i18n.messages.js is out of date." >&2
    echo "i18n: run: uv run python scripts/build_i18n.py --write  (then commit both files)" >&2
    exit 1
fi

# i18n coverage ratchet (item 4): read-only, silent on success. A hardcoded string
# in a migrated namespace, an unknown/dead id, or a stale allowlist entry refuses
# the boot rather than serving copy the CSV does not own.
if ! i18n_check_out=$(uv run python scripts/i18n_check.py --quiet 2>&1); then
    printf '%s\n' "$i18n_check_out" >&2
    echo "i18n: refusing to boot — the copy coverage gate failed." >&2
    echo "i18n: run: uv run python scripts/i18n_check.py" >&2
    exit 1
fi

# Say out loud which database is about to be opened: silently migrating or
# mutating ./data is how the live DB got written mid-round (PLAN-v2 §8.2 F5).
# Exported so the server, the scripts and any child process agree on one path
# (DATABASE_URL, if set, still wins — see app/database.py).
RESOLVED_DATA_DIR="${HCRM_DATA_DIR:-$PWD/data}"
HCRM_DATA_DIR="$RESOLVED_DATA_DIR"
export HCRM_DATA_DIR
if [ -n "$DEV_ADMIN" ]; then DEV_ADMIN_LABEL="ON (admin/admin)"; else DEV_ADMIN_LABEL="off"; fi
echo "database:  ${DATABASE_URL:-sqlite:///$RESOLVED_DATA_DIR/hcrm.db}"
echo "bind:      http://$HOST:$PORT   dev admin: $DEV_ADMIN_LABEL"
if [ "$SCRATCH" = 1 ]; then
    # Unmissable by design: an operator with a stale HCRM_SCRATCH=1 once read
    # the old two-line banner as "the shop catalogue is broken". Only say what
    # is unconditionally true — no claim about ./data (HCRM_DATA_DIR may point
    # elsewhere) and none about "the shop" (DATABASE_URL may select it); the
    # "database:" line above reports the database actually opened.
    echo "scratch:   ON — experiments (HCRM_SCRATCH=1); unset it to run the shop"
    echo "           this boot's data dir: $RESOLVED_DATA_DIR"
else
    echo "scratch:   off"
fi
if [ "$SCRATCH" != 1 ]; then
    if [ ! -e "$RESOLVED_DATA_DIR/hcrm.db" ]; then
        echo "           (no database yet — first boot will create, migrate and seed it)"
    else
        echo "           LIVE DATA — back it up first with scripts/backup_db.sh"
    fi
fi

# --- process composition: the M-10 daemon comes up with the app ----------
# run.sh is the kiosk's one-command start, so it starts both processes the
# operator needs: uvicorn, then (unless this is a scratch run, or the
# operator opted out) the scanner grab daemon. The trap stops the daemon
# FIRST — its SIGTERM handler closes the fd, releasing the EVIOCGRAB — then
# the server. A dead daemon is degraded, never fatal: the app boots and
# serves without a scanner, and registration precedes the grab, so the pill
# still pairs. `--forward` is ALWAYS loopback: the register/pair gates hold
# even when HCRM_HOST=0.0.0.0. No HCRM_SCANNER_*_ANY flag is ever set.
SERVER_PID=""
DAEMON_PID=""

_reap() {  # zombie-safe: `jobs -pr` lists live children only, `kill -0` does not
    [ -n "$1" ] || return 0
    # Only a LIVE child is ever signalled: a dead/reaped pid must not be
    # TERMed — the number could already belong to a recycled process.
    jobs -pr | grep -qx "$1" || return 0
    kill -TERM "$1" 2>/dev/null || return 0
    for _ in $(seq 50); do
        jobs -pr | grep -qx "$1" || return 0
        sleep 0.1
    done
    kill -KILL "$1" 2>/dev/null || true
}
shutdown() {
    rc=$?
    trap - EXIT INT TERM
    _reap "$DAEMON_PID"; wait "$DAEMON_PID" 2>/dev/null || true
    _reap "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null || true
    exit "$rc"
}
# EXIT preserves uvicorn's status; INT/TERM select 130/143 and then run that
# same cleanup, so a child exiting first cannot turn a signal into status 0.
trap shutdown EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# --- scanner bring-up: the read-only verdict, never fatal (C3) -------------
# The daemon below forwards under this host's resolved identity; the preflight
# echoes --find-scanner's FAIL/WARN/verdict lines onto this same console so a
# stale identity or an absent device is loud at boot, not only in an opt-in
# tool. It runs BEFORE the daemon spawns, so the report never describes our
# own lock/grab, and a non-zero verdict NEVER aborts the boot — degraded-but-up
# is the existing contract.
_scanner_wait_server() {  # $1 = forward URL; 0 once it accepts connections
    # One bounded stdlib poll: no curl dependency, and the URL is parsed by
    # urllib, so `http://[::1]:PORT` needs no shell surgery (bash /dev/tcp
    # cannot parse the IPv6 bracket form). Without this wait a cold boot
    # races uvicorn's bind and the report prints a false `server UNREACHABLE`
    # FAIL from inside the script that is starting the server.
    uv run python -c '
import socket, sys, time
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
host, port = url.hostname, url.port or 80
if not host:
    sys.exit(1)
deadline = time.monotonic() + 10.0
while time.monotonic() < deadline:
    try:
        socket.create_connection((host, port), timeout=1).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.2)
sys.exit(1)' "$1" 2>/dev/null
}

_server_alive() {  # zombie-safe: `jobs -pr` lists LIVE children only
    # `kill -0` cannot be trusted here: a child that exited but was not reaped
    # yet still answers it, and bash only reaps on SIGCHLD. The same idiom the
    # reap helper uses is the truth.
    jobs -pr | grep -qx "$SERVER_PID"
}

_scanner_verdict() {  # $1 config, $2 pairing file, $3 forward URL
    # Bounded when timeout(1) exists (Linux/CI); stock macOS ships no
    # timeout, and then the tool's own 5 s HTTP timeout is the only bound.
    # --kill-after covers the uv -> python child: a survivor holding the
    # pipe would otherwise outlive the bound inside $( ).
    if command -v timeout >/dev/null 2>&1; then
        timeout -k 5 30 uv run python scripts/scanner_daemon.py --find-scanner \
            --config "$1" --pairing-file "$2" --forward "$3" 2>&1
    else
        uv run python scripts/scanner_daemon.py --find-scanner \
            --config "$1" --pairing-file "$2" --forward "$3" 2>&1
    fi
}

uv run uvicorn app.main:app --host "$HOST" --port "$PORT" &
SERVER_PID=$!

SCANNER_DAEMON="${HCRM_SCANNER_DAEMON:-1}"
case "$SCANNER_DAEMON" in 0|false|FALSE|no|off) SCANNER_DAEMON="" ;; esac
if [ -z "$SCANNER_DAEMON" ]; then
    echo "scanner:   daemon disabled (HCRM_SCANNER_DAEMON=${HCRM_SCANNER_DAEMON:-1})"
elif [ "$SCRATCH" = 1 ]; then
    echo "scanner:   daemon skipped (scratch run — the real scanner stays alone)"
else
    # The forward URL is ALWAYS loopback (the register/pair gates): [::1]
    # for IPv6-only binds, 127.0.0.1 otherwise. A bind to one specific
    # non-loopback address leaves no loopback listener for the daemon —
    # warn and skip rather than forward to a LAN address (that would need
    # the HCRM_SCANNER_*_ANY flags, which this script never sets).
    case "$HOST" in
        ::1|::) SCANNER_FORWARD="http://[::1]:$PORT" ;;
        127.0.0.1|localhost|0.0.0.0|"") SCANNER_FORWARD="http://127.0.0.1:$PORT" ;;
        *)
            echo "scanner:   daemon skipped — HCRM_HOST=$HOST gives the daemon no 127.0.0.1/[::1] listener to forward to (use HCRM_HOST=127.0.0.1 or 0.0.0.0)"
            SCANNER_FORWARD="" ;;
    esac
    if [ -n "$SCANNER_FORWARD" ]; then
        SCANNER_CONFIG="$RESOLVED_DATA_DIR/scanner.json"
        SCANNER_PAIRING_FILE="${HCRM_SCANNER_PAIRING_FILE:-$RESOLVED_DATA_DIR/scanner-pairing.json}"
        # Skipped on a first boot (no config yet — the daemon is registering
        # right now, so the report could only say "no config"). rc 0/1 is a
        # completed report (1 = blocking findings, the point); rc >= 2 means
        # it did not complete (e.g. 124 from timeout) and is said in words.
        if [ -f "$SCANNER_CONFIG" ]; then
            # Our own uvicorn first: a bare TCP connect can be answered by a
            # FOREIGN listener on this port (a leftover server from another
            # checkout), and the verdict would then describe that process
            # while reading OUR database. Checked again right before the
            # report runs, so a server that died during the wait is never
            # mistaken for a healthy one. Never fatal either way.
            if ! _server_alive; then
                echo "scanner:   preflight skipped — our uvicorn is not running"
            elif ! _scanner_wait_server "$SCANNER_FORWARD"; then
                echo "scanner:   preflight skipped — $SCANNER_FORWARD not accepting connections within 10 s; run: uv run python scripts/scanner_daemon.py --find-scanner"
            elif ! _server_alive; then
                echo "scanner:   preflight skipped — our uvicorn is not running"
            else
                SCANNER_RC=0
                SCANNER_REPORT="$(_scanner_verdict "$SCANNER_CONFIG" \
                    "$SCANNER_PAIRING_FILE" "$SCANNER_FORWARD")" || SCANNER_RC=$?
                # rc 0/1 are completed reports (1 = blocking findings, the
                # point). rc 1 WITHOUT a verdict line is a crash (a Python
                # traceback exits 1) and rc >= 2 did not complete — both are
                # said in words, with the head of the output, and never fatal.
                if [ "$SCANNER_RC" -gt 1 ]; then
                    echo "scanner:   preflight did not complete (rc=$SCANNER_RC) — run: uv run python scripts/scanner_daemon.py --find-scanner"
                    printf '%s\n' "$SCANNER_REPORT" | head -3 \
                        | sed 's/^/scanner:   | /' || true
                elif ! printf '%s\n' "$SCANNER_REPORT" | grep -q 'verdict:'; then
                    echo "scanner:   preflight produced no verdict (rc=$SCANNER_RC) — run: uv run python scripts/scanner_daemon.py --find-scanner"
                    printf '%s\n' "$SCANNER_REPORT" | head -3 \
                        | sed 's/^/scanner:   | /' || true
                else
                    # Only FAIL/WARN/verdict: the diagnosis, not the INFO
                    # chatter and not the printed recovery ladder.
                    printf '%s\n' "$SCANNER_REPORT" \
                        | grep -E '^(FAIL|WARN) |verdict:' \
                        | sed 's/^/scanner:   /' || true
                fi
            fi
        fi
        uv run python scripts/scanner_daemon.py \
            --config "$SCANNER_CONFIG" \
            --forward "$SCANNER_FORWARD" \
            --pairing-file "$SCANNER_PAIRING_FILE" \
            --register-wait "${HCRM_SCANNER_REGISTER_WAIT:-300}" &
        DAEMON_PID=$!
        echo "scanner:   daemon pid $DAEMON_PID (config $SCANNER_CONFIG)"
    fi
fi

STATUS=0
wait "$SERVER_PID" || STATUS=$?   # set -e-safe: capture uvicorn's exit code
SERVER_PID=""                     # the EXIT path must never signal this pid again
exit "$STATUS"                    # fires the EXIT trap, which re-exits with $?
