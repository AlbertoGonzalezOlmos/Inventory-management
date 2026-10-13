"""ORM models for the shop (SQLite + sqlite-vector)."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, LargeBinary, String, UniqueConstraint
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
    # SHA-256 hash of the QR badge credential (app.qrbadge), None when the
    # account has no badge. Like session tokens, the raw badge secret is
    # never persisted — a leaked database must not yield printable badges.
    qr_badge_hash: str | None = Field(default=None, index=True)
    # Badge derivation generation (Task A): 0 = a random (legacy/pre-key)
    # badge that cannot be re-derived; >= 1 = derived from the master secret
    # (app/security.py) and re-viewable via GET /api/auth/qr-badge.
    # Monotonic per account: rotation increments, generations are never
    # reused — a revoked-then-reissued badge never equals an older one.
    qr_badge_generation: int = Field(default=0)
    # Contact phone (Task B), stored NORMALISED (optional leading '+',
    # 6-15 digits — see app/schemas.py::normalise_phone). NULL = not set;
    # "" on input clears (the ItemPatch.barcode precedent). Not unique
    # (households share numbers); not a credential: changing it needs only
    # a live session (changing the EMAIL — the login identifier — is the
    # password-gated path).
    phone: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)

    @property
    def has_qr_badge(self) -> bool:
        return self.qr_badge_hash is not None


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
    # Canonical GTIN-14 of the product's retail barcode (unique, nullable).
    # Every EAN/UPC representation normalises to this form on input
    # (app/barcodes.py), so one physical product cannot get two entries
    # through its EAN-13 and its UPC-E, say. NULL = no retail barcode
    # (internal SKU-labelled stock); SQLite unique indexes allow many NULLs.
    barcode: str | None = Field(
        default=None,
        sa_column=Column("barcode", String(14), unique=True, index=True,
                         nullable=True),
    )
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


class CartItem(SQLModel, table=True):
    """One line of a user's server-side shopping cart (purchase list).

    The cart is per-user and durable — every mutation is its own committed
    request — so scans and browsers converge on one list, and the badge-driven
    account switch can guarantee "whatever had been done is saved" by
    construction. The old browser-localStorage list was user-agnostic and not
    even cleared on logout, so it could never support either.

    Live prices only: no price snapshot column. Totals are computed at read
    time from ``items.price_cents`` (checkout, the only consumer of "price at
    time of order", does not exist yet; a snapshot would be write-only data
    with a staleness reconciliation problem).

    FKs are declarative only (``PRAGMA foreign_keys`` is OFF,
    app/database.py D4): the item/user API delete paths clean up rows
    explicitly, and every cart read lazily reaps rows orphaned by any other
    path (hand-written SQL, races).
    """

    __tablename__ = "cart_items"

    # One row per (user, item): "already in cart" is an increment, never a
    # duplicate line — and this constraint is the conflict target of the
    # single-statement upsert in app/routers/cart.py. Declared from day one:
    # SQLite cannot add a unique constraint to a live table later.
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_cart_items_user_item"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    item_id: int = Field(foreign_key="items.id", index=True)
    # No DB CHECK and no Field bound: SQLModel does not validate table models,
    # and a CHECK(qty > 0) would fight a decrement-then-delete write path.
    # Quantity bounds are the API schemas' job (app/schemas.py, CartAddIn
    # & co); accumulation beyond them is capped in the upsert itself.
    qty: int
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class ScanRequest(SQLModel, table=True):
    """A member's proposal for a catalogue entry from an unresolvable scan.

    Staff-only catalogue creation (coordinator decision 1): a member at the
    counter gets the intake form, and filing it persists THIS request for
    staff to resolve — by creating the item through the normal staff-only
    path (POST /api/items, prefilled from gtin14) and linking it back, or by
    dismissing it. The member never writes the catalogue.

    Classification fields are captured AT FILING time (server-derived, never
    trusted from the client) so the request stays interpretable after
    barcode rules or the catalogue change.
    """

    __tablename__ = "scan_requests"

    id: int | None = Field(default=None, primary_key=True)
    requester_id: int = Field(foreign_key="users.id", index=True)
    code: str = Field(max_length=64)
    symbology: str = ""
    # analyze_scan snapshot at filing: kind + canonical key.
    kind: str = ""
    gtin14: str | None = None
    # The member's proposal (what the intake modal collected).
    name: str
    price_cents: int = 0
    status: str = Field(default="open", index=True)  # open|resolved|dismissed
    # Plain int, deliberately NOT a FK: a resolved request is a historical
    # audit record and must survive the item's later deletion (weak ref;
    # read paths tolerate the dangling id with a null join result).
    resolved_item_id: int | None = None
    resolved_by: int | None = None
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class Purchase(SQLModel, table=True):
    """A completed checkout (Task C): an immutable receipt.

    The cart is intent; THIS is the sale — the only member-facing flow that
    touches stock (coordinator decision 2). Created atomically by
    POST /api/orders together with its lines and the cart clear.
    """

    __tablename__ = "purchases"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    # Σ price_cents × applied_qty at checkout — what was actually paid, never
    # what was merely requested (you don't pay for undelivered goods).
    total_cents: int = 0
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class PurchaseLine(SQLModel, table=True):
    """One line of a purchase receipt — a DENORMALISED SNAPSHOT: what was
    actually bought, at the checkout-time price. History must never rewrite
    when the catalogue changes (price edits) or disappear (item deletion)."""

    __tablename__ = "purchase_lines"

    id: int | None = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchases.id", index=True)
    # Plain int, deliberately NOT a FK: history survives item deletion (the
    # scan_requests.resolved_item_id weak-ref precedent).
    item_id: int | None = None
    sku: str
    name: str
    # The snapshot: live at checkout time.
    price_cents: int
    qty: int            # the ORDERED amount (what the member wanted)
    applied_qty: int    # what was actually deliverable (clamped at stock)
    clamped: bool = False
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class AuthToken(SQLModel, table=True):
    """A bearer token issued at login."""

    __tablename__ = "auth_tokens"

    token: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    # Indexed: _issue_token() purges expired rows on every login, so an
    # unindexed expires_at made that a full table scan per login
    # (PLAN-v2 §8.2 N8). Existing databases get the index in _migrate_schema(),
    # because create_all() never alters a table it finds.
    expires_at: datetime = Field(sa_type=DateTime, index=True)
    # Server-enforced kiosk logoff countdown (Task 4): when set, this token
    # dies at this instant even if the browser vanishes mid-countdown —
    # lazily enforced in get_current_user (reject-and-delete). NULL = no
    # countdown armed. Per-TOKEN, not per-user: the user's other sessions
    # are unaffected (the same principle as logout).
    logoff_deadline: datetime | None = Field(default=None, sa_type=DateTime)


class AppMeta(SQLModel, table=True):
    """Tiny key/value table for one-shot maintenance markers.

    Used to bound the cost of the well-known-password scan (app/main.py):
    verifying a hash is a full PBKDF2 run, so scanning every account on every
    boot would scale badly. A new table needs no migration — create_all() adds
    tables it does not find (it only ever fails to ALTER existing ones).
    """

    __tablename__ = "app_meta"

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class Terminal(SQLModel, table=True):
    """A scanner kiosk: one grab daemon + one browser (round 4, T4).

    The counter-session binding of the scan queue: the daemon (T2/T3,
    scripts/scanner_daemon.py) holds the terminal SECRET (write credential
    — it ingests scans), the kiosk browser holds the TOKEN (read credential
    — it polls the queue, logged out included, so badge login works from
    the login screen). Terminal-auth, not user-auth, is what lets the
    logged-out screen receive scans.

    Every credential is stored as its SHA-256 only (app.security.token_hash,
    the session-token doctrine): a leaked database yields no usable secret,
    token or pairing code. The 8-char pairing code is minted at
    registration, verified constant-time at pairing, and is single-use by
    construction: once the terminal has a token, pairing answers 409.

    ``last_seen_at`` tracks the DAEMON (register + every ingest) — the
    browser's polls do not touch it, so "daemon online" stays truthful.
    """

    __tablename__ = "terminals"

    id: int | None = Field(default=None, primary_key=True)
    terminal_id: str = Field(index=True, unique=True)
    # The daemon's write credential (presented at register, then per ingest).
    secret_hash: str
    # The browser's read credential (None until the pairing code is used).
    token_hash: str | None = Field(default=None)
    # Single-use pairing code hash (minted at register; see class docstring).
    pairing_code_hash: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    last_seen_at: datetime | None = Field(default=None, sa_type=DateTime)


class ScanEvent(SQLModel, table=True):
    """One scanned code in a terminal's queue (the daemon→SPA channel).

    ``seq`` is a global autoincrement and therefore strictly increasing —
    it IS the polling cursor (``?after=``), scoped per terminal by the
    terminal_id filter. ``kind`` is ALWAYS derived server-side
    (app.scanshapes.classify_scan) — a client-supplied kind is never
    accepted, the scan_requests doctrine: the wire carries codes, the
    server decides what they are.

    Lifetime is bounded because badge payloads are bearer credentials:
    consumed rows are pruned on the next touch, everything older than
    SCANNER_EVENT_TTL_SECONDS (app/routers/scanner.py) on every touch —
    a scan nobody consumed must never replay into a later session.
    """

    __tablename__ = "scan_events"
    # sqlite_autoincrement: the PK must be monotonic FOREVER. A plain
    # INTEGER PRIMARY KEY restarts at 1 once pruning empties the table —
    # a new event would then get seq <= a live client's cursor and never
    # be delivered (caught by the at-most-once test). AUTOINCREMENT makes
    # SQLite track the high-water mark in sqlite_sequence instead.
    __table_args__ = {"sqlite_autoincrement": True}

    seq: int | None = Field(default=None, primary_key=True)
    terminal_id: str = Field(index=True)
    code: str
    # "badge" | "barcode" | "unshaped" — server-derived (see class docstring).
    kind: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    # Set in the same commit that returns the event to a poller: at-most-once
    # delivery (two browser tabs can never double-add one scan), and the
    # prune marker (consumed rows are deleted on the next touch).
    consumed_at: datetime | None = Field(default=None, sa_type=DateTime,
                                         index=True)
