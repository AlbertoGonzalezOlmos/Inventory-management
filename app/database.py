"""Database engine and session management (SQLite + sqlite-vector extension).

The sqlite-vector extension (https://github.com/sqliteai/sqlite-vector) is
loaded into every pooled connection. Item embeddings are stored as float32
BLOBs in the ordinary `items.embedding` column and searched with the
extension's SIMD-accelerated `vector_full_scan` (exact cosine distance).
"""

import importlib.resources
import logging
import os

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from app.embeddings import EMBEDDING_DIM

logger = logging.getLogger("hcrm")

DATA_DIR = os.environ.get(
    "HCRM_DATA_DIR",
    str(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")),
)
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(DATA_DIR, 'hcrm.db')}",
)

# Options for the sqlite-vector extension: exact cosine distance over 384-dim
# float32 vectors. Set via vector_init() on each connection that searches.
VECTOR_OPTIONS = f"dimension={EMBEDDING_DIM},type=FLOAT32,distance=COSINE"


def _extension_path() -> str:
    return str(importlib.resources.files("sqlite_vector.binaries") / "vector")


engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},  # needed for SQLite + FastAPI
)


@event.listens_for(engine, "connect")
def _load_vector_extension(dbapi_connection, _connection_record):
    """Load sqlite-vector into every new pooled connection."""
    # WAL lets readers proceed while a writer holds the database; the busy
    # timeout makes concurrent writers wait instead of erroring immediately.
    dbapi_connection.execute("PRAGMA journal_mode=WAL")
    dbapi_connection.execute("PRAGMA busy_timeout=5000")

    dbapi_connection.enable_load_extension(True)
    dbapi_connection.load_extension(_extension_path())
    dbapi_connection.enable_load_extension(False)

    # Register the embedding column for search (table may not exist yet on a
    # fresh database; the search endpoint re-runs vector_init anyway).
    from app import models  # noqa: F401  (registers tables)

    table_exists = dbapi_connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
    ).fetchone()
    if table_exists:
        dbapi_connection.execute(
            f"SELECT vector_init('items', 'embedding', '{VECTOR_OPTIONS}')"
        )


# NOTE on transaction mode: the driver default (implicit BEGIN around DML
# only, SELECTs in autocommit) is deliberate and load-bearing.
#
# - Read-only requests never take a write lock, so a burst of logins cannot
#   starve catalogue reads (the incident this section documents).
# - Each write transaction starts at its first DML statement, so it always
#   sees the latest committed state — which is what makes the last-admin
#   TRIGGERS below race-free without any explicit locking: SQLite serialises
#   writers, and the trigger's WHEN subquery evaluates at write time.
#
# Do NOT add a "begin" event hook emitting BEGIN IMMEDIATE here: an earlier
# revision did exactly that and turned every request (reads included) into a
# write-lock holder, producing unauthenticated DoS ("database is locked"
# 500s) under concurrent load. The last-admin invariant is enforced by the
# triggers, not by transaction mode.


def get_session():
    """FastAPI dependency yielding a database session."""
    with Session(engine) as session:
        yield session


# Defense in depth for the last-admin guarantee: even if a code path (or a
# hand-written SQL statement) bypasses the API checks, the database itself
# refuses any UPDATE/DELETE that would leave zero admins. The API checks in
# app/routers/members.py still run first to produce friendly 400s; these
# triggers are the race-free backstop (SQLite serialises writers).
TRIGGER_DDL = (
    """
    CREATE TRIGGER IF NOT EXISTS users_keep_last_admin_role
    BEFORE UPDATE OF role ON users
    WHEN OLD.role = 'admin' AND NEW.role <> 'admin'
         AND (SELECT COUNT(*) FROM users WHERE role = 'admin') <= 1
    BEGIN
        SELECT RAISE(ABORT, 'Cannot demote the last admin');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS users_keep_last_admin_delete
    BEFORE DELETE ON users
    WHEN OLD.role = 'admin'
         AND (SELECT COUNT(*) FROM users WHERE role = 'admin') <= 1
    BEGIN
        SELECT RAISE(ABORT, 'Cannot delete the last admin');
    END
    """,
)


def create_all() -> None:
    """Create tables and safety triggers if they do not exist yet."""
    from app import models  # noqa: F401  (registers tables)

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        for ddl in TRIGGER_DDL:
            conn.execute(text(ddl))


def vector_extension_available() -> bool:
    """Check that the sqlite-vector extension is loaded and working."""
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT vector_version()")).scalar() is not None
    except Exception:
        return False
