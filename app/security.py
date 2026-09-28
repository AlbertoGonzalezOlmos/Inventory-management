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

# Contract for W4.1 (reject well-known passwords as *new* passwords):
# "changeme" is always rejected; DEV_ADMIN_PASSWORD must be rejected whenever
# DEV_ADMIN is off, and is only settable by the seeder while it is on. Note
# the API schemas keep min_length=8, so no endpoint can set a 5-char password
# anyway — the dev credential is seed-only by construction.

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
