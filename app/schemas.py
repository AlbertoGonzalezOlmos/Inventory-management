"""API request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Pragmatic email check (dependency-free alternative to EmailStr).
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


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

    The payload is returned exactly once — afterwards only its hash exists
    server-side, so a lost badge means generating a new one (which revokes
    the old one automatically).
    """

    payload: str
    svg: str


class ChangePasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: str
    must_change_password: bool = False
    has_qr_badge: bool = False
    created_at: datetime


class AuthOut(BaseModel):
    token: str
    user: UserOut


# --- Items ---------------------------------------------------------------

class ItemIn(_Stripped):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    category: str = "general"
    price_cents: int = Field(default=0, ge=0)
    stock: int = Field(default=0, ge=0)
    image_url: str = ""


class ItemPatch(_PatchIn):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    category: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)
    image_url: str | None = None


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
    has_embedding: bool = False
    created_at: datetime
    updated_at: datetime


class VectorSearchOut(ItemOut):
    """Item plus cosine similarity (1 = identical direction) to the query."""

    similarity: float


# --- Members ---------------------------------------------------------------

class MemberIn(_Stripped):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=255, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)
    role: str = "member"  # "member" | "staff" | "admin" (admin role: admins only)


class MemberPatch(_PatchIn):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = None
    # Password reset — only admins may set this (enforced in the router).
    password: str | None = Field(default=None, min_length=8, max_length=128)
