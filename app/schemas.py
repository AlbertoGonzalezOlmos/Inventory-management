"""API request/response schemas."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.barcodes import normalize_input_gtin

# Pragmatic email check (dependency-free alternative to EmailStr).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def normalise_phone(value: str | None) -> str | None:
    """Canonicalise a phone number (Task B): strip whitespace and the visual
    separators space/-/./(), keep an optional single leading '+', then
    require 6–15 digits. None/'' → None (CLEAR — the ItemPatch.barcode
    ""-clears precedent). Anything else → ValueError (→ 422): a typo must
    not become a contact identity. Shape validation only — no carrier/
    region truth (no SMS/OTP in scope).

    Stored form examples: '+34 600 123 456' → '+34600123456';
    '(555) 123-4567' → '5551234567'. Deterministic equality, so no-op
    comparisons are exact.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("phone must be a string")
    cleaned = re.sub(r"[\s\-().]", "", value.strip())
    if not cleaned:
        return None
    plus = cleaned.startswith("+")
    digits = cleaned[1:] if plus else cleaned
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        raise ValueError(
            "phone must be 6-15 digits with an optional leading '+' "
            "(spaces, dashes, dots and parentheses are ignored); got " + repr(value)
        )
    return ("+" + digits) if plus else digits


def _normalized_gtin_or_none(value):
    """Canonicalise a user-supplied barcode to GTIN-14 (or None when empty).

    Accepts any EAN/UPC representation (EAN-13, EAN-8, UPC-A, 8-digit UPC-E
    with preamble, GTIN-14) and stores the zero-padded 14-digit form, so the
    same product scanned/typed in different formats can never create two
    catalogue entries. Raises ValueError (→ 422) on a bad check digit or
    implausible length — a typo must not silently become an identity.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("barcode must be a string of digits")
    value = value.strip()
    if not value:
        return None
    gtin = normalize_input_gtin(value)
    if gtin is None:
        raise ValueError(
            "barcode must be a valid GTIN (EAN-13/EAN-8/UPC-A/UPC-E/GTIN-14 "
            "with a correct check digit); got " + repr(value)
        )
    return gtin


class _Stripped(BaseModel):
    """Input schema base: strips surrounding whitespace *before* validation,
    and rejects unknown fields (a typo like price_cent must not be silently
    ignored behind a 200 OK)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class _PatchIn(_Stripped):
    """PATCH schema base: explicit nulls are rejected (422).

    Without this, {"name": null} would pass exclude_unset() and be setattr'd
    onto a NOT NULL column, surfacing as a 500 at commit time. Null is not a
    valid "leave unchanged" for any field here — omit the key instead.
    """

    @model_validator(mode="before")
    @classmethod
    def _reject_nulls(cls, data):
        if isinstance(data, dict):
            nulls = sorted(k for k, v in data.items() if v is None and k in cls.model_fields)
            if nulls:
                raise ValueError(
                    f"null is not allowed for field(s): {', '.join(nulls)} — "
                    "omit the field to leave it unchanged"
                )
        return data


# --- Auth ---------------------------------------------------------------

class RegisterIn(_Stripped):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=255, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=255)
    password: str = Field(max_length=128)


class QrLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The full badge payload ("HCRM1:<token>") as scanned; the raw token
    # without prefix is also accepted (see app.qrbadge.parse_badge_payload).
    token: str = Field(min_length=1, max_length=64)


class QrBadgeOut(BaseModel):
    """A freshly generated badge: the secret payload and its printable form.

    The payload is returned exactly once at issuance — afterwards it is
    re-viewable forever via GET (derived from the server's master key, Task
    A); a lost badge can also always be rotated (which revokes the old one
    automatically).
    """

    payload: str
    svg: str


class QrBadgeViewOut(QrBadgeOut):
    """A RE-VIEWED badge (repeatable): the payload + SVG + its generation,
    and whether this view auto-migrated a legacy (random) badge — in which
    case the previously printed card stopped working and the UI must say so.
    ``status`` is the house envelope discriminator ("ok" | "migrated"),
    matching the error envelopes' {status: "none"|"no_key"|"key_mismatch"}.
    """

    status: str
    generation: int
    migrated: bool = False


class QrIdentifyOut(BaseModel):
    """The answer to "is this scanned badge the CALLER's own?" — one bit,
    no side effects, no enumeration (a non-matching input — garbage, a
    product barcode, somebody else's badge — all answer the same false)."""

    same_user: bool


class LogoffScheduleOut(BaseModel):
    """The armed kiosk logoff countdown: the server-computed deadline (the
    SPA counts down from THIS, never from a client-side now+30 — clock
    skew and request latency are not allowed near the truth) and the
    configured grace, echoed for display."""

    logoff_deadline: datetime
    grace_seconds: int


class ChangePasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ChangePhoneIn(_Stripped):
    """Set (or clear) the caller's phone. Contact info, not a credential:
    session-only, NO password re-entry (a stolen session can at worst
    change a contact field — the same exposure class as saying it at the
    counter). Single-field command body: both "" and null CLEAR (there is
    no omit-semantics here, unlike MemberPatch's patch document).
    Idempotent: setting the current value is a 200, not an error (no
    credential semantics, no revocation — contrast ChangeEmailIn's no-op
    400)."""

    # Required key; the VALUE may be null or "" (both clear).
    phone: str | None = Field(max_length=32)

    @field_validator("phone", mode="before")
    @classmethod
    def _canonical_phone(cls, v):
        return normalise_phone(v)


class ChangeEmailIn(BaseModel):
    """Change the caller's EMAIL — the login identifier and therefore the
    account-takeover surface: the CURRENT PASSWORD is required (a stolen
    session must not be able to move the login identity), and every OTHER
    session is revoked (change-password parity). A no-op (new == current
    after normalisation) is a 400: it must not look like a change, and a
    "successful" one would revoke every other session for nothing. The
    badge is NEVER touched (derivation keys on user_id, Task A).

    Deliberately NOT _Stripped: that would strip `current_password` too,
    and the password must reach verify_password RAW — exactly as LoginIn
    and ChangePasswordIn (both strip-free) deliver it, so a password with
    leading/trailing spaces keeps working here. Only `new_email` is
    stripped, before the pattern gate (mirroring RegisterIn's _Stripped
    behaviour: a padded email is normalised, not rejected).
    """

    model_config = ConfigDict(extra="forbid")
    new_email: str = Field(min_length=3, max_length=255, pattern=EMAIL_PATTERN)
    current_password: str = Field(min_length=1, max_length=128)

    @field_validator("new_email", mode="before")
    @classmethod
    def _strip_email(cls, v):
        return v.strip() if isinstance(v, str) else v


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: str
    must_change_password: bool = False
    has_qr_badge: bool = False
    # Contact phone, normalised (None = not set). Visible to self (/me) and
    # to staff (the members list) — the shop's own member directory, the
    # same exposure class as email.
    phone: str | None = None
    created_at: datetime


class AuthOut(BaseModel):
    token: str
    user: UserOut


class RegisterOut(AuthOut):
    """Registration result: the session PLUS the account's one-time badge.

    The field is required (not optional-on-AuthOut): registration always
    issues a badge, and a forgotten assignment must fail schema validation
    rather than serialise as null. AuthOut itself is untouched — it is also
    the *login* (password and qr-login) response, and login responses must
    never carry badge material: a badge is shown exactly once, at creation
    or rotation.
    """

    qr_badge: QrBadgeOut


class MemberCreatedOut(UserOut):
    """Staff-side account creation: UserOut plus the one-time badge, so the
    counter can print the membership card immediately. The members list,
    /me and PATCH responses keep the plain UserOut shape — no badge
    material outside creation and rotation."""

    qr_badge: QrBadgeOut


# --- Items ---------------------------------------------------------------

class ItemIn(_Stripped):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    category: str = "general"
    price_cents: int = Field(default=0, ge=0)
    stock: int = Field(default=0, ge=0)
    image_url: str = ""
    # Canonical GTIN-14 of the retail barcode, or null for SKU-only stock.
    barcode: str | None = None

    @field_validator("barcode", mode="before")
    @classmethod
    def _canonical_barcode(cls, v):
        return _normalized_gtin_or_none(v)


class ItemPatch(_PatchIn):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    category: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)
    image_url: str | None = None
    # "" clears the barcode (explicit null is rejected by _PatchIn like every
    # other field here); any valid GTIN representation replaces it.
    barcode: str | None = None

    @field_validator("barcode", mode="before")
    @classmethod
    def _canonical_barcode(cls, v):
        return _normalized_gtin_or_none(v)


class StockAdjustIn(_Stripped):
    """A relative stock delta (scanner/POS/receiving traffic).

    Zero is rejected: a no-op adjustment is a client bug (an unparsed flag,
    a dropped sign), never a meaningful request, and it must not return 200
    looking like something happened.
    """

    delta: int = Field(ge=-100_000, le=100_000)

    @field_validator("delta")
    @classmethod
    def _non_zero(cls, v):
        if v == 0:
            raise ValueError("delta must be non-zero")
        return v


class StockAdjustOut(BaseModel):
    """The result of one atomic stock adjustment.

    ``applied_delta`` differs from ``requested_delta`` exactly when the
    adjustment was clamped at 0 — a sale beyond available stock is reported,
    never silently dropped (REVIEW-m10.md P6).
    """

    id: int
    sku: str
    stock: int
    requested_delta: int
    applied_delta: int
    clamped: bool


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    description: str
    category: str
    price_cents: int
    stock: int
    image_url: str
    barcode: str | None = None
    has_embedding: bool = False
    created_at: datetime
    updated_at: datetime


class VectorSearchOut(ItemOut):
    """Item plus cosine similarity (1 = identical direction) to the query."""

    similarity: float


class SuggestOut(BaseModel):
    """Suggestion groups for the unknown-item intake flow. All groups carry
    plain ItemOut (a uniform pick-list; similarity scores are noise for
    suggestions). by_semantic degrades to [] — never a 503."""

    by_prefix: list[ItemOut]
    by_keyword: list[ItemOut]
    by_semantic: list[ItemOut]


# --- Members ---------------------------------------------------------------

class MemberIn(_Stripped):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=255, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    role: str = "member"  # "member" | "staff" | "admin" (admin role: admins only)
    # Contact phone, optional at counter signup; normalised ("" → None).
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("phone", mode="before")
    @classmethod
    def _canonical_phone(cls, v):
        return normalise_phone(v)


class MemberPatch(_PatchIn):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = None
    # Password reset — only admins may set this (enforced in the router).
    password: str | None = Field(default=None, min_length=8, max_length=128)
    # Contact phone (Task B): staff may edit a member's phone. Omit =
    # unchanged (exclude_unset), explicit null = 422 (_PatchIn's rule — a
    # patch document has omit-semantics, unlike ChangePhoneIn's command
    # body), "" clears. NEVER email: staff must not move a member's login
    # identifier (a takeover vector) — the field is absent and
    # extra="forbid" makes any attempt a 422.
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("phone", mode="before")
    @classmethod
    def _canonical_phone(cls, v):
        return normalise_phone(v)


# --- Cart -----------------------------------------------------------------

# Quantity bounds: a hard API bound so a garbage client gets a 422, never a
# silent clamp; accumulation *across* requests is capped where it happens,
# inside the upsert (app/routers/cart.py, QTY_CAP).
CART_QTY_MIN = 1
CART_QTY_MAX = 10_000
CART_MAX_LINES = 200


class CartAddIn(_Stripped):
    """Add-or-increment one cart line (the UI/scanner add path)."""

    item_id: int
    qty: int = Field(default=1, ge=CART_QTY_MIN, le=CART_QTY_MAX)


class CartSetIn(_Stripped):
    """Absolute quantity set for an existing line; 0 removes it."""

    qty: int = Field(ge=0, le=CART_QTY_MAX)


class CartReplaceLineIn(_Stripped):
    item_id: int
    qty: int = Field(ge=CART_QTY_MIN, le=CART_QTY_MAX)


class CartReplaceIn(_Stripped):
    """Whole-cart replace (the flush/replay primitive for the account-switch
    flow and the one-time localStorage import). All-or-nothing: any unknown
    item or duplicate line rejects the request, leaving the cart untouched."""

    lines: list[CartReplaceLineIn] = Field(max_length=CART_MAX_LINES)


class CartLineOut(BaseModel):
    """One cart line with LIVE catalogue data (price, stock) — never a
    snapshot: the totals follow the shop's current prices at read time."""

    item: ItemOut
    qty: int
    line_total_cents: int
    created_at: datetime
    updated_at: datetime


class CartOut(BaseModel):
    lines: list[CartLineOut]
    total_qty: int
    total_cents: int


class CartScanIn(_Stripped):
    """One scanned code, resolved against the catalogue by exact identity
    and added to the caller's cart on a match."""

    code: str = Field(min_length=1, max_length=64)  # fits HCRM1: payloads (49)
    symbology: str = Field(default="", max_length=32)  # scanner-reported, optional
    qty: int = Field(default=1, ge=CART_QTY_MIN, le=CART_QTY_MAX)


class CartScanOut(BaseModel):
    """A successful scan-to-cart: what matched, the resulting line, and the
    full cart (the Task-1 mutator contract — one payload, no read-after-write)."""

    status: str  # "added"
    match: str   # "barcode" | "sku"
    item: ItemOut
    qty: int     # the resulting line quantity
    cart: CartOut


# --- Scan requests ------------------------------------------------------------

class ScanRequestIn(_Stripped):
    """A member's 'unknown item' report: the scanned code plus the name/price
    the intake modal collected. The classification is re-derived server-side
    — a client-supplied kind is never trusted."""

    code: str = Field(min_length=1, max_length=64)
    symbology: str = Field(default="", max_length=32)
    name: str = Field(min_length=1, max_length=255)
    price_cents: int = Field(default=0, ge=0)


class RequesterOut(BaseModel):
    """The filer of a scan request — public fields only (staff queue view)."""

    id: int
    name: str
    email: str


class ScanRequestOut(BaseModel):
    id: int
    requester: RequesterOut
    code: str
    symbology: str
    kind: str
    gtin14: str | None
    name: str
    price_cents: int
    status: str
    resolved_item_id: int | None
    resolved_by: int | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScanRequestResolveIn(_Stripped):
    """Staff resolution: link the (already created) catalogue item. When
    add_to_cart is set, the item is also added to the requester's cart —
    the loop-closing option; default off (resolution is administrative)."""

    item_id: int
    add_to_cart: bool = False


# --- Purchases (Task C) --------------------------------------------------------


class PurchaseLineOut(BaseModel):
    """A receipt line: the checkout-time snapshot, with the ordered amount
    (qty), what was actually deliverable (applied_qty), and whether the sale
    was clamped at stock. Clamp-not-veto: a shortfall sells what is
    available and the receipt records BOTH numbers."""

    item_id: int | None
    sku: str
    name: str
    price_cents: int
    qty: int
    applied_qty: int
    clamped: bool
    line_total_cents: int


class PurchaseOut(BaseModel):
    """A purchase with its lines — the immutable receipt."""

    id: int
    created_at: datetime
    total_cents: int
    lines: list[PurchaseLineOut]


class CheckoutOut(BaseModel):
    """The receipt of a checkout: the purchase, the emptied cart (the SPA
    re-renders from one payload — the Task-1 mutator contract), and the
    item_ids of orphaned cart lines that were skipped (their items no
    longer exist; the checkout proceeded for the rest)."""

    purchase: PurchaseOut
    cart: CartOut
    skipped: list[int] = []
