"""ORM models for the shop (SQLite + sqlite-vector)."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, LargeBinary
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Naive UTC timestamp (friendly to SQLAlchemy/PostgreSQL comparisons)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(SQLModel, table=True):
    """A person with an account: member (buyer), staff, or admin."""

    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    password_hash: str
    role: str = Field(default="member")  # "member" | "staff" | "admin"
    # True while the account uses a password it must change (seeded default
    # password or an admin-issued reset). Enforced in get_current_user.
    must_change_password: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class Item(SQLModel, table=True):
    """An article in the shop catalogue, with an embedding of its description."""

    __tablename__ = "items"

    id: int | None = Field(default=None, primary_key=True)
    sku: str = Field(index=True, unique=True)
    name: str = Field(index=True)
    description: str = ""
    category: str = Field(default="general", index=True)
    price_cents: int = 0
    stock: int = 0
    image_url: str = ""
    # Embedding of name+description as a float32 BLOB (sqlite-vector format).
    embedding: bytes | None = Field(
        default=None,
        sa_column=Column("embedding", LargeBinary, nullable=True),
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)

    @property
    def has_embedding(self) -> bool:
        return self.embedding is not None


class AuthToken(SQLModel, table=True):
    """A bearer token issued at login."""

    __tablename__ = "auth_tokens"

    token: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    expires_at: datetime = Field(sa_type=DateTime)
