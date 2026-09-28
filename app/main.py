"""FastAPI application entrypoint.

Serves the JSON API under /api and the Vue single-page frontend from /static.
Run with:  uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app import models  # noqa: F401  (registers tables)
from app.database import create_all, engine, vector_extension_available
from app.embeddings import model_status, warm_up
from app.routers import auth, items, members
from app.seed import backfill_embeddings, seed_example_items
from app.security import (
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEV_ADMIN,
    DEV_ADMIN_PASSWORD,
    DEV_ADMIN_USERNAME,
    PBKDF2_ITERATIONS,
    WELL_KNOWN_PASSWORDS,
    hash_password,
    verify_password,
)

logger = logging.getLogger("hcrm")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


def _ensure_dev_admin(session: Session) -> None:
    """Idempotently ensure the dev admin credential exists (owner directive).

    Creates the account when it is missing; NEVER touches an existing account's
    password or role, so re-running it cannot undo an operator's changes.
    """
    if not DEV_ADMIN:
        return
    existing = session.exec(
        select(models.User).where(models.User.email == DEV_ADMIN_USERNAME)
    ).first()
    if existing is not None:
        return
    session.add(
        models.User(
            email=DEV_ADMIN_USERNAME,
            name="Dev Administrator",
            password_hash=hash_password(DEV_ADMIN_PASSWORD),
            role="admin",
            # The credential must work for testing/debugging — a flagged account
            # is blocked from the API until it changes its password, which would
            # defeat the directive.
            must_change_password=False,
            created_at=models.utcnow(),
        )
    )
    session.commit()
    logger.warning(
        "SECURITY: dev admin mode is ON (HCRM_DEV_ADMIN) — username '%s' with "
        "password '%s' has full admin access. Loopback-only; run.sh refuses a "
        "network bind unless HCRM_ALLOW_INSECURE_BIND=1. Set HCRM_DEV_ADMIN=0 "
        "for the secure default.",
        DEV_ADMIN_USERNAME,
        DEV_ADMIN_PASSWORD,
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
        _ensure_dev_admin(session)
        seed_example_items(session)
        backfill_embeddings(session)
        _flag_well_known_passwords(session)
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


WEAK_SCAN_KEY = "well_known_password_scan"


def _scan_marker() -> str:
    """Identity of the current scan policy, stored in app_meta.

    Includes dev-admin mode, because the mode decides whether the well-known
    `admin` account is exempt: turning HCRM_DEV_ADMIN off must re-run the scan
    so that account gets flagged like any other.
    """
    return ",".join(sorted(WELL_KNOWN_PASSWORDS)) + ("|dev-admin" if DEV_ADMIN else "")


def _flag_well_known_passwords(session: Session) -> None:
    """Flag every account that still uses a password published by this repo.

    A boot-time warning does not close the `changeme` class of issue — the flag
    does: the account is blocked from the API until the password is changed.

    Scope (PLAN-v2 §8.2 N5): this used to inspect only DEFAULT_ADMIN_EMAIL, so
    any *other* account sitting on a well-known password (reachable: an admin
    reset to `changeme` returned 200, and change-password to the same value
    cleared the flag — §0 B10) was never flagged. It now scans every account,
    with two exclusions:
      - already-flagged accounts (nothing to gain, and it skips the PBKDF2 cost);
      - the W4.0 dev admin while HCRM_DEV_ADMIN is on — well-known by owner
        directive; flagging it would block the credential that exists to be used.

    Cost: verifying a hash is a full PBKDF2 run per account per candidate
    password, so the scan is bounded by a marker row in `app_meta` and runs once
    per database per well-known-password set (i.e. again only if that set
    changes). Accounts created afterwards cannot hold a well-known password:
    every endpoint that sets one rejects them (W4.1).
    """
    marker = _scan_marker()
    meta = session.get(models.AppMeta, WEAK_SCAN_KEY)
    if meta is not None and meta.value == marker:
        return

    started = time.monotonic()
    flagged: list[str] = []
    checked = 0
    for user in session.exec(select(models.User)).all():
        if user.must_change_password:
            continue
        if DEV_ADMIN and user.email == DEV_ADMIN_USERNAME:
            continue
        checked += 1
        if any(verify_password(pw, user.password_hash) for pw in WELL_KNOWN_PASSWORDS):
            user.must_change_password = True
            session.add(user)
            flagged.append(user.email)

    session.add(models.AppMeta(key=WEAK_SCAN_KEY, value=marker,
                               updated_at=models.utcnow()))
    session.commit()
    for email in flagged:
        logger.warning(
            "SECURITY: %s uses a password published by this project — the account "
            "is blocked from the API until it is changed (Account → Change "
            "password).", email,
        )
    logger.info(
        "Well-known-password scan: %d account(s) checked, %d flagged in %.2fs.",
        checked, len(flagged), time.monotonic() - started,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(_startup)
    yield


# Interactive docs are dev-only tooling: they load jsdelivr assets and an
# inline bootstrap script, which the app CSP below forbids. They are exempt
# from the CSP header, or disabled outright with HCRM_DOCS=0. openapi_url
# MUST be nulled together with docs_url/redoc_url: FastAPI gates all three
# on openapi_url, so nulling only the two visible ones leaves /openapi.json
# (the full endpoint schema) public (PLAN-v2 §1, verified empirically).
DOCS_ENABLED = os.environ.get("HCRM_DOCS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

app = FastAPI(
    title="HCRM - Shop Catalogue & Members",
    version="0.3.0",
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

app.include_router(auth.router)
app.include_router(items.router)
app.include_router(members.router)

# Cached at startup; computed lazily on first call when the lifespan did not
# run (e.g. the test suite).
_vector_status: bool | None = None


# Docs-only paths: exempt from the CSP header (they load jsdelivr assets
# and an inline bootstrap script; see DOCS_ENABLED above).
_DOCS_PATHS = ("/docs", "/redoc")
_OPENAPI_PATH = "/openapi.json"


def _is_docs_path(path: str) -> bool:
    """True for the interactive-docs routes, which are exempt from the CSP.

    Bounded match on purpose: `/docs` and anything *under* it
    (`/docs/oauth2-redirect`), but NOT `/docs.html`. A bare
    `startswith("/docs")` exempted every static file whose name begins with
    "docs" — measured: a `static/docs.html` was served with no
    Content-Security-Policy header at all (PLAN-v2 §8.2 F3). The exemption is a
    security boundary, so it gets a boundary.
    """
    if path == _OPENAPI_PATH:
        return True
    return any(path == p or path.startswith(p + "/") for p in _DOCS_PATHS)

# Why 'unsafe-eval' is unavoidable: the frontend deliberately uses Vue's
# FULL build (vendored vue.global.prod.js, no build step) with an in-DOM
# template, so the runtime compiler generates render functions via
# new Function() — which script-src 'self' alone forbids. A policy without
# 'unsafe-eval' shipped here once and blanked the entire SPA (PLAN-v2 §0,
# B2). The cost: 'unsafe-eval' substantially negates the CSP's protection
# against injected scripts; the tracked endgame is precompiled render
# functions (PLAN-v2 W7.2, README "Tracked issues").
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def apply_security_headers(response: Response, path: str) -> Response:
    """Attach the hardening headers (single source of truth).

    Used by the middleware below AND by the 500 handler: an exception raised
    inside an endpoint propagates straight through `BaseHTTPMiddleware` (the
    code after `call_next` never runs), so without that handler a 500 carries
    none of these headers — measured, and the previous docstring claimed
    otherwise (PLAN-v2 §9.5).
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if not _is_docs_path(path):
        response.headers["Content-Security-Policy"] = CSP_POLICY
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline browser hardening on every response this middleware sees.

    CSP is skipped only for the docs routes (dev tooling; see DOCS_ENABLED)
    — everything the product actually serves, including the SPA and every
    API response, carries the full policy. 'unsafe-inline' in style-src is
    for Vue's :style bindings (the analysis bar chart), not for scripts.
    Unhandled exceptions bypass this middleware; `unhandled_error` covers those.
    """
    response: Response = await call_next(request)
    return apply_security_headers(response, request.url.path)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """A 500 must be as hardened as a 200.

    Starlette sends this response and then re-raises, so uvicorn still logs the
    traceback. (Headers cannot be added once a response has started streaming;
    that case is out of reach by construction.)
    """
    return apply_security_headers(
        JSONResponse(status_code=500, content={"detail": "Internal server error"}),
        request.url.path,
    )


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
    return {
        "ok": True,
        "vector": _vector_status,
        "embeddings": model_status(),
        # Loud, machine-readable exposure of the dev credential mode (W4.0):
        # a deployment can be checked for it without reading logs.
        "insecure_dev_admin": DEV_ADMIN,
        # Not a secret, and load tests need it: a latency budget is meaningless
        # without the hashing cost it was measured at (PLAN-v2 §8.2 N10).
        "pbkdf2_iterations": PBKDF2_ITERATIONS,
    }

# Frontend: hash-based routing means a plain static mount is enough.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
