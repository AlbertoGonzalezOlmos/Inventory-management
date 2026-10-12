"""Password hashing and token helpers (stdlib only, no external crypto deps)."""

import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from app.models import AuthToken, utcnow

# Overridable via env so the test suite can run with cheap hashing.
PBKDF2_ITERATIONS = int(os.environ.get("HCRM_PBKDF2_ITERATIONS", "600000"))
TOKEN_TTL = timedelta(days=7)


# --- Badge derivation master secret (Task A) ----------------------------------
#
# QR badge payloads are DERIVED from this secret (app/qrbadge.py) so they can
# be re-viewed forever. The secret is a bearer-grade credential: database +
# key together can derive every member's badge (the accepted trade-off — see
# docs/opticon-m10.md §9.4). It therefore lives OUTSIDE the database, and a
# backup of one without the other is incomplete.

BADGE_SECRET_ENV = "HCRM_BADGE_SECRET"
BADGE_KEY_FILENAME = "badge.key"
# Any badge secret (env or keyfile) must be at least this long: the derivation
# key is bearer-grade; a truncated one must fail loudly, not silently fork the
# derivation space.
BADGE_SECRET_MIN_LEN = 32


def _badge_data_dir() -> str:
    """The data dir, computed WITHOUT importing app.database (that would pull
    app.embeddings/fastembed into every security consumer for one path
    constant). Keep in sync with app.database.DATA_DIR's default."""
    return os.environ.get(
        "HCRM_DATA_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    )


# Snapshot at import (the house pattern for every HCRM_* knob). A SET but
# invalid (short/empty) value fails FAST at import: falling back to the
# keyfile would silently derive with a different key than the operator
# intended — every such badge would 409 on re-view once the env is fixed.
_env_badge_secret = os.environ.get(BADGE_SECRET_ENV)
if _env_badge_secret is not None and len(_env_badge_secret.strip()) < BADGE_SECRET_MIN_LEN:
    raise SystemExit(
        f"{BADGE_SECRET_ENV} is set but shorter than {BADGE_SECRET_MIN_LEN} "
        "characters — refusing to start. Deriving badges with a truncated "
        "secret would silently fork the derivation space; remove the "
        "variable (the keyfile is then used) or set a proper secret."
    )

# Lazy cache: (secret, source) once loaded; None-not-loaded vs loaded-None is
# the tuple itself. Tests monkeypatch this attribute (or pass an explicit
# secret to the qrbadge functions).
_badge_secret_state: tuple[str | None, str | None] | None = None


def _read_keyfile(path: str) -> str | None:
    try:
        with open(path) as f:
            value = f.read().strip()
        return value if len(value) >= BADGE_SECRET_MIN_LEN else None
    except OSError:
        return None


def _load_badge_secret() -> tuple[str | None, str | None]:
    """Resolve the secret: env (verbatim, never written to disk) → keyfile
    (auto-generated on first use, 0600, O_EXCL so concurrent boots race
    safely: the loser re-reads the winner's file) → None (data dir
    unwritable / corrupt file). None never breaks authentication: logins
    verify by hash and need no key; it disables badge RE-VIEWING and makes
    issuance fall back to random legacy badges."""
    if _env_badge_secret is not None and _env_badge_secret.strip():
        return _env_badge_secret.strip(), "env"

    path = os.path.join(_badge_data_dir(), BADGE_KEY_FILENAME)
    existing = _read_keyfile(path)
    if existing is not None:
        return existing, "file"

    # Auto-generate — deliberately, so the server always boots (a missing
    # key must never lock anyone out; the loss is detected later at re-view
    # time by the hash mismatch, never by a boot crash).
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        secret = secrets.token_urlsafe(32)  # 43 chars — same shape as badge tokens
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret)
        return secret, "file"
    except FileExistsError:
        # A concurrent boot won the race: read the winner's file.
        winner = _read_keyfile(path)
        return (winner, "file") if winner is not None else (None, None)
    except OSError:
        return None, None


def badge_secret() -> str | None:
    """The badge-derivation master secret (cached per process), or None."""
    global _badge_secret_state
    if _badge_secret_state is None:
        _badge_secret_state = _load_badge_secret()
    return _badge_secret_state[0]


def badge_secret_source() -> str | None:
    """\"env\" | \"file\" | None — for healthz/startup visibility."""
    badge_secret()  # ensure loaded
    return _badge_secret_state[1]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- Dev admin mode (owner directive, round 5: "until further notice") -------
# A well-known credential for testing/debugging. This deliberately weakens the
# default-password controls, so it is an explicit, loud and reversible MODE,
# not a silent default:
#   - announced at startup and reported by /api/healthz (insecure_dev_admin);
#   - scripts/run.sh refuses a non-loopback bind while it is on
#     (override: HCRM_ALLOW_INSECURE_BIND=1);
#   - the test suite runs with HCRM_DEV_ADMIN=0 (tests/conftest.py), so every
#     security test still exercises the secure default.
# Turn it off with HCRM_DEV_ADMIN=0.
DEV_ADMIN = _env_flag("HCRM_DEV_ADMIN", True)
DEV_ADMIN_USERNAME = "admin"
DEV_ADMIN_PASSWORD = "admin"

# The seeded administrator credential. It lives here rather than in the
# entrypoint because the routers (which must reject it as a *new* password) and
# the boot-time flagger both need it (PLAN.md item 5 / PLAN-v2 W4.1).
DEFAULT_ADMIN_EMAIL = "admin@shop.local"
DEFAULT_ADMIN_PASSWORD = "changeme"

# Passwords published by this repository (README + seed code). They are never
# acceptable as a *chosen* password — setting one used to be the easy way back
# to a well-known credential: an admin reset to `changeme` returned 200, and
# `change-password changeme -> changeme` returned 204 *and cleared*
# must_change_password, leaving a default-password account with full API access
# (PLAN-v2 §0 B10). The dev credential is seed-only by construction: it is
# written by _ensure_dev_admin(), never through an endpoint (every write path
# enforces min_length=8, and `admin` is 5).
WELL_KNOWN_PASSWORDS = frozenset({DEFAULT_ADMIN_PASSWORD, DEV_ADMIN_PASSWORD})


def is_well_known_password(password: str) -> bool:
    """True for a password published by this repo (reject it with a 400)."""
    return password in WELL_KNOWN_PASSWORDS


# The kiosk logoff countdown grace (Task 4). Env-overridable for demos and
# tests; read once at import like the other HCRM_* knobs. Clamped >= 1 at
# parse so a zero/negative env value cannot make scheduling an instant
# self-logout; tests that need zero grace monkeypatch the module attribute
# (app.security.LOGOFF_GRACE_SECONDS) at call time instead — the router
# reads the module attribute precisely so that override works.
LOGOFF_GRACE_SECONDS = max(
    1, int(os.environ.get("HCRM_LOGOFF_GRACE_SECONDS", "60"))
)

# A syntactically valid hash that never matches. Login verifies against this
# when the email is unknown, so response time does not reveal whether an
# account exists (user-enumeration timing side-channel).
DUMMY_HASH = f"pbkdf2_sha256${PBKDF2_ITERATIONS}${'0' * 32}${'0' * 64}"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    # A malformed stored hash must never raise (500) — just fail the login.
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def token_hash(raw_token: str) -> str:
    """SHA-256 of a bearer token.

    Only the hash is persisted: a leaked database file must not yield usable
    sessions. (Upgrade note: tokens issued before this change were stored in
    plaintext and simply stop matching — everyone re-logs-in once.)
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


def new_token(user_id: int) -> tuple[AuthToken, str]:
    """Create a token record (hash stored) and return it with the raw secret."""
    raw = secrets.token_urlsafe(32)
    record = AuthToken(
        token=token_hash(raw),
        user_id=user_id,
        expires_at=utcnow() + TOKEN_TTL,
    )
    return record, raw
