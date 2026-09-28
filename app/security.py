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
