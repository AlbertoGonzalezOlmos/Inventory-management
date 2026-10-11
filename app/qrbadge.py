"""QR badge credentials: payload format and SVG rendering.

A QR badge is a physical, printable login credential for an account — an
alternative to typing email+password, meant to be read by the Opticon M-10
(see docs/opticon-m10.md §9, and §10 for the kiosk scan flows) or any 2D
scanner/camera.

Payload format:  HCRM1:<urlsafe-token>
  - the ``HCRM1:`` prefix versions the format and lets scanners/bridges tell
    a badge apart from an ordinary product barcode;
  - the token is 43 chars of urlsafe [A-Za-z0-9_-] — since Task A it is
    DERIVED: HMAC-SHA256(badge master secret, "badge:{user_id}:{generation}")
    truncated to 32 bytes, so a badge can be re-viewed forever by
    recomputing it (only its SHA-256 hash is stored, and the hash remains
    the sole login authority). The master secret lives OUTSIDE the database
    (env or data/badge.key — see app/security.py): database + key together
    can derive every member's badge, which is the documented trade-off of
    re-viewability (docs/opticon-m10.md §9.4); a database leak alone still
    yields nothing printable. Legacy random badges (pre-derivation) verify
    by hash indefinitely and auto-migrate to a derived badge on first
    re-view.
  - a badge is a bearer credential, so only its SHA-256 hash is stored
    server-side (same treatment as session tokens, see
    app.security.token_hash).

The QR code itself is generated with the vendored Nayuki encoder
(app/vendor/qrcodegen.py) — pure Python, no native deps, works offline.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from app import security
from app.security import token_hash
from app.vendor.qrcodegen import QrCode

BADGE_PREFIX = "HCRM1:"

# Domain-separation prefix for the derivation message — FROZEN FOREVER: any
# change to the derivation formula silently orphans every stored badge hash
# (pinned by the known-answer test in tests/test_badge_derivation.py; a
# deliberate change is a mass-reissue event — see docs/opticon-m10.md §9.4).
BADGE_DERIVATION_CONTEXT = "badge"

# Error-correction level M (~15% damage tolerance): printed badges get
# scuffed, and the payload is short enough that the code stays small.
_ECC = QrCode.Ecc.MEDIUM


def new_badge_payload() -> str:
    """Generate a fresh badge payload (the secret printed inside the QR)."""
    return BADGE_PREFIX + secrets.token_urlsafe(32)


def parse_badge_payload(scanned: str) -> str | None:
    """Validate a scanned/typed string as a badge payload.

    Returns the normalised payload, or None if it is not a badge. The M-10
    sends exactly what is printed (no AIM symbology identifier by default),
    so a strict prefix check is enough to keep product barcodes out of the
    login endpoint.
    """
    scanned = scanned.strip()
    if not scanned.startswith(BADGE_PREFIX):
        return None
    token = scanned[len(BADGE_PREFIX):]
    # token_urlsafe(32) -> 43 chars of [A-Za-z0-9_-]; reject anything else so
    # a truncated/garbled scan fails cleanly instead of hitting the database.
    if len(token) != 43 or not all(c.isalnum() or c in "-_" for c in token):
        return None
    return scanned


def derive_badge_payload(user_id: int, generation: int, secret: str) -> str:
    """The derived badge payload — a pure, frozen function:

        "HCRM1:" + base64url(HMAC-SHA256(secret, "badge:{user_id}:{generation}")[:32])

    43 urlsafe characters — byte-identical in shape to the legacy random
    ``token_urlsafe(32)`` output, so ``parse_badge_payload``, the bridge's
    prefix check and the SPA's shape gates accept derived badges unchanged.
    Keyed on ``user_id`` (immutable) and NEVER on email/name: identity edits
    can never change a badge.
    """
    digest = hmac.new(
        secret.encode(),
        f"{BADGE_DERIVATION_CONTEXT}:{user_id}:{generation}".encode(),
        hashlib.sha256,
    ).digest()
    return BADGE_PREFIX + base64.urlsafe_b64encode(digest[:32]).decode().rstrip("=")


def new_badge_for(user, secret: str | None = None) -> str:
    """Issue the user's NEXT badge and store only its hash.

    Derivation-aware (Task A): with a secret, ``generation++`` and derive —
    generations are monotonic and never reused, so a revoked-then-reissued
    badge can never equal an older one. WITHOUT a secret (key unavailable:
    unwritable data dir / corrupt keyfile), issue a random LEGACY payload
    and leave the generation UNTOUCHED: badge issuance must never block
    account creation, and the generation must never be decremented — see
    the fallback comment below (credential resurrection).

    Contract (Task A): derivation needs ``user.id`` — for a NEW user the
    caller must ``session.add(user)`` and ``session.flush()`` FIRST (the
    flush assigns the id without committing, so the whole issuance — row,
    generation and hash — still rides in ONE transaction and a raced insert
    rolls back everything together). Raises ValueError on an unflushed user
    when a secret is available: loud, per house culture.

    Nothing is committed here — the caller's transaction decides. Returns
    the raw payload; only its SHA-256 ever reaches the database (the login
    authority — ``qr-login``/``qr-identify`` never need the key).
    """
    if secret is None:
        secret = security.badge_secret()  # module attribute: one monkeypatch point
    if secret is None:
        # Graceful degradation: a random legacy-shaped payload. The
        # generation is deliberately left UNTOUCHED — never reset to 0:
        # resetting a derived badge's generation would let a later
        # auto-migration re-derive an ALREADY-RETIRED payload (same id,
        # same generation, restored key ⇒ the same HMAC ⇒ a dead printed
        # credential resurrected). With the generation preserved, re-view
        # after the key returns answers key_mismatch (hash of the random
        # payload ≠ the derivation) and the healing rotation bumps to an
        # unused generation. New users keep the model default 0 = legacy
        # (auto-migrates to generation 1 on first re-view).
        payload = new_badge_payload()
    else:
        if user.id is None:
            raise ValueError(
                "flush the user (assign its id) before issuing a derived badge — "
                "derivation is keyed on user.id"
            )
        generation = (user.qr_badge_generation or 0) + 1
        payload = derive_badge_payload(user.id, generation, secret)
        user.qr_badge_generation = generation
    user.qr_badge_hash = token_hash(payload)
    return payload


def rederive_and_check(user, secret: str | None = None) -> str | None:
    """Recompute the payload for the user's stored generation and return it
    ONLY if ``token_hash(payload) == user.qr_badge_hash`` — the derivation
    scheme's self-test. None on: no derived badge (generation 0), no key, or
    a mismatch (rotated/swapped key, hand-edited row). A mismatch must never
    render a badge: the re-view endpoints answer None with a structured 409.
    """
    if not user.qr_badge_hash or not user.qr_badge_generation or user.qr_badge_generation < 1:
        return None
    if secret is None:
        secret = security.badge_secret()
    if secret is None:
        return None
    payload = derive_badge_payload(user.id, user.qr_badge_generation, secret)
    # compare_digest: house doctrine for credential comparisons
    # (verify_password) — both sides are ASCII hex, as required for str.
    return payload if hmac.compare_digest(token_hash(payload), user.qr_badge_hash) else None


@dataclass
class BadgeView:
    """The domain result of a badge re-view; the routers map it to the
    status-convention envelopes (404 none / 409 no_key / 409 key_mismatch /
    200 ok | migrated)."""

    status: str                     # "ok" | "migrated" | "none" | "no_key" | "key_mismatch"
    payload: str | None = None
    generation: int = 0
    migrated: bool = False


def view_badge(session, user, secret: str | None = None) -> BadgeView:
    """Re-view a user's badge — repeatable, forever (Task A).

    - no badge (hash None)             → "none"
    - no key available                 → "no_key" (logins unaffected)
    - derived (generation >= 1)        → verify-then-return: the recomputed
      hash must equal the stored one, else "key_mismatch" — a rotated or
      corrupt key can NEVER render a wrong badge
    - legacy random badge (generation 0) → AUTO-MIGRATE (coordinator
      decision): the random payload is unrecoverable by construction, so the
      only re-viewable badge is a fresh derived one — issue generation 1,
      commit, return "migrated". The old printed badge dies (the UI warns).
      Idempotent and race-safe by determinism: two concurrent first views
      both derive generation 1 → the SAME payload and hash (unlike a random
      rotate, double-fire cannot double-rotate).
    """
    if not user.qr_badge_hash:
        return BadgeView(status="none")
    if secret is None:
        secret = security.badge_secret()
    if secret is None:
        return BadgeView(status="no_key")

    if user.qr_badge_generation and user.qr_badge_generation >= 1:
        # Verify-then-return via the shared self-test (single source for
        # the derived-hash comparison): None here can only mean MISMATCH —
        # the no-hash/no-key/gen-0 cases are handled above.
        payload = rederive_and_check(user, secret)
        if payload is None:
            return BadgeView(status="key_mismatch")
        return BadgeView(status="ok", payload=payload,
                         generation=user.qr_badge_generation)

    # Legacy random badge: auto-migrate to a derived generation-1 badge.
    payload = new_badge_for(user, secret)
    session.add(user)
    session.commit()
    return BadgeView(status="migrated", payload=payload,
                     generation=user.qr_badge_generation, migrated=True)


def badge_svg(payload: str, border: int = 2) -> str:
    """Render a payload as an SVG QR code (dark modules only, on white).

    Inline SVG keeps printing and display dependency-free: no PNG encoder,
    no image files, scales to any badge size. CSP-safe (markup, no script).
    """
    qr = QrCode.encode_text(payload, _ECC)
    size = qr.get_size()
    parts: list[str] = []
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                parts.append(f"M{x},{y}h1v1h-1z")
    dimension = size + border * 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dimension} {dimension}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR login badge">'
        f'<rect width="{dimension}" height="{dimension}" fill="#fff"/>'
        f'<path d="{"".join(parts)}" fill="#000" transform="translate({border},{border})"/>'
        f"</svg>"
    )
