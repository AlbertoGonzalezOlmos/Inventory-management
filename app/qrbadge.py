"""QR badge credentials: payload format and SVG rendering.

A QR badge is a physical, printable login credential for an account — an
alternative to typing email+password, meant to be read by the Opticon M-10
(see docs/opticon-m10.md §10) or any 2D scanner/camera.

Payload format:  HCRM1:<urlsafe-token>
  - the ``HCRM1:`` prefix versions the format and lets scanners/bridges tell
    a badge apart from an ordinary product barcode;
  - the token is 256 bits of randomness (``secrets.token_urlsafe(32)``) —
    a bearer credential, so only its SHA-256 hash is stored server-side
    (same treatment as session tokens, see app.security.token_hash).

The QR code itself is generated with the vendored Nayuki encoder
(app/vendor/qrcodegen.py) — pure Python, no native deps, works offline.
"""

import secrets

from app.vendor.qrcodegen import QrCode

BADGE_PREFIX = "HCRM1:"

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
