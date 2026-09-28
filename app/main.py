"""FastAPI application entrypoint.

Serves the JSON API under /api and the Vue single-page frontend from /static.
Run with:  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app import models  # noqa: F401  (registers tables)
from app.database import create_all, engine, vector_extension_available
from app.embeddings import model_status, warm_up
from app.routers import auth, items, members
from app.seed import backfill_embeddings, seed_example_items
from app.security import hash_password, verify_password

logger = logging.getLogger("hcrm")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DEFAULT_ADMIN_EMAIL = "admin@shop.local"
DEFAULT_ADMIN_PASSWORD = "changeme"


def seed_default_admin(session: Session) -> None:
    """Create a default admin account on first run (when no users exist)."""
    any_user = session.exec(select(models.User).limit(1)).first()
    if any_user is not None:
        return
    admin = models.User(
        email=DEFAULT_ADMIN_EMAIL,
        name="Shop Administrator",
        password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
        role="admin",
        must_change_password=True,  # enforced until the password is changed
        created_at=models.utcnow(),
    )
    session.add(admin)
    session.commit()
    logger.warning(
        "Seeded default admin account: %s / %s  (change this password!)",
        DEFAULT_ADMIN_EMAIL,
        DEFAULT_ADMIN_PASSWORD,
    )


def _startup() -> None:
    """Synchronous startup work (runs in a threadpool: embedding/backfill can
    take a while and must not block the event loop)."""
    # Load and validate the embedding model early (HCRM_EMBED_STRICT=1 aborts
    # the boot on a dimension mismatch instead of silently degrading).
    warm_up()
    create_all()
    _migrate_schema()
    with Session(engine) as session:
        seed_default_admin(session)
        seed_example_items(session)
        backfill_embeddings(session)
        _flag_default_password(session)
    # Cache the extension probe so /api/healthz never touches the database:
    # under load that turned a busy DB into a false "vector: false".
    global _vector_status
    _vector_status = vector_extension_available()


def _migrate_schema() -> None:
    """SQLModel's create_all never ALTERs existing tables — add columns here."""
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if "must_change_password" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN must_change_password "
                "BOOLEAN NOT NULL DEFAULT 0"
            )
            logger.info("Added users.must_change_password column (migration).")


def _flag_default_password(session: Session) -> None:
    """Force a password change on the admin while the default is still in use.

    A boot-time warning does not close the changeme class of issue — the flag
    does: the account is blocked from the API until the password is changed.
    """
    admin = session.exec(
        select(models.User).where(models.User.email == DEFAULT_ADMIN_EMAIL)
    ).first()
    if admin is not None and verify_password(DEFAULT_ADMIN_PASSWORD, admin.password_hash):
        if not admin.must_change_password:
            admin.must_change_password = True
            session.add(admin)
            session.commit()
        logger.warning(
            "SECURITY: %s still uses the default password — the account is "
            "blocked from the API until the password is changed "
            "(Account → Change password).",
            DEFAULT_ADMIN_EMAIL,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(_startup)
    yield


app = FastAPI(
    title="HCRM - Shop Catalogue & Members",
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(items.router)
app.include_router(members.router)

# Cached at startup; computed lazily on first call when the lifespan did not
# run (e.g. the test suite).
_vector_status: bool | None = None


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline browser hardening. CSP allows inline style *attributes* only
    because Vue binds :style for the analysis bar chart; scripts are strictly
    same-origin (everything is vendored under /static/vendor).
    """
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return response


@app.get("/api/healthz", tags=["meta"])
def healthz():
    """Liveness/readiness probe.

    Uses the startup-cached extension check (never touches the DB, so lock
    contention cannot flip it) and reports the embedding model state without
    triggering a load.
    """
    global _vector_status
    if _vector_status is None:
        _vector_status = vector_extension_available()
    return {"ok": True, "vector": _vector_status, "embeddings": model_status()}

# Frontend: hash-based routing means a plain static mount is enough.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
