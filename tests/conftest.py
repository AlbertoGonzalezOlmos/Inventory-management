"""Pytest fixtures for the HCRM API tests.

Every test gets a fresh database (drop/recreate/reseed), so tests are
order-independent and cannot poison each other via shared state.

The embedding model is NEVER loaded: warm_up is neutralised and embed calls
are replaced with fast, deterministic fakes (one-hot vectors derived from a
hash of the text), so tests need neither the ONNX model, its first-run
download, nor network access.

IMPORTANT: the environment must be configured before any `app.*` import,
because the database URL is computed at import time.
"""

import hashlib
import os
import tempfile

# setdefault: pytest can import this module twice (as `conftest` and as
# `tests.conftest`); the second execution must not re-point the env at a
# different temp dir than the one app.database already imported.
os.environ.setdefault("HCRM_DATA_DIR", tempfile.mkdtemp(prefix="hcrm-test-"))
os.environ.setdefault("HCRM_PBKDF2_ITERATIONS", "1000")  # cheap hashing for tests
# The dev admin credential (W4.0, HCRM_DEV_ADMIN, default ON for the owner's
# testing directive) is OFF for the suite: every security test must keep
# exercising the secure default. tests/test_dev_admin.py covers the mode itself
# in a subprocess, because the flag is read at import time.
os.environ.setdefault("HCRM_DEV_ADMIN", "0")

import pytest

import app.embeddings
import app.main
import app.routers.items as items_router
import app.seed
from app.embeddings import EMBEDDING_DIM, vec_to_blob

ADMIN_EMAIL = "admin@shop.local"
ADMIN_DEFAULT_PASSWORD = "changeme"
# The admin_token fixture performs the mandatory first password change; this
# is the password the rest of the suite uses for the seeded admin.
ADMIN_PASSWORD_AFTER_CHANGE = "admin-password-1"


def get_db_path() -> str:
    """The database file the app is actually using (not re-derived from env)."""
    import app.database

    return os.path.join(app.database.DATA_DIR, "hcrm.db")


def _fake_vec(text: str) -> list[float]:
    """Deterministic one-hot 'embedding' — cheap but vector-search compatible."""
    idx = int(hashlib.md5(text.encode()).hexdigest(), 16) % EMBEDDING_DIM
    vec = [0.0] * EMBEDDING_DIM
    vec[idx] = 1.0
    return vec


def _fake_embed_text(text: str) -> list[float] | None:
    if not text or not text.strip():
        return None
    return _fake_vec(text)


def _fake_embed_text_blob(text: str) -> bytes | None:
    vec = _fake_embed_text(text)
    return vec_to_blob(vec) if vec is not None else None


# Patch at module level (before any seeding runs): the routers and seed
# module resolve these names from their own module namespace at call time.
items_router.embed_text = _fake_embed_text
items_router.embed_text_blob = _fake_embed_text_blob
app.seed.embed_text_blob = _fake_embed_text_blob
app.embeddings.embed_text = _fake_embed_text
app.embeddings.embed_text_blob = _fake_embed_text_blob

# Never load the real ONNX model during tests (on a clean machine warm_up
# would download ~80 MB): the fakes above cover every embed call.
app.main.warm_up = lambda: None


@pytest.fixture(scope="session")
def _client():
    """A single TestClient, no lifespan: each test seeds its own database."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _reset_db() -> None:
    """Drop and recreate everything, then reseed the canonical fixture data."""
    from sqlmodel import Session, SQLModel

    from app.database import create_all, engine
    from app.main import _migrate_schema, seed_default_admin
    from app.seed import backfill_embeddings, seed_example_items

    SQLModel.metadata.drop_all(engine)
    create_all()  # also (re)creates the last-admin triggers
    _migrate_schema()
    with Session(engine) as session:
        seed_default_admin(session)
        seed_example_items(session)
        backfill_embeddings(session)


@pytest.fixture()
def client(_client):
    _reset_db()
    yield _client


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client) -> str:
    """Token for the seeded admin, with the mandatory password change done.

    The change clears must_change_password while keeping this session alive
    (change-password spares the current session), so the token works for the
    rest of the test.
    """
    r = client.post(
        "/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_DEFAULT_PASSWORD},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["must_change_password"] is True
    token = r.json()["token"]
    r = client.post(
        "/api/auth/change-password",
        json={
            "current_password": ADMIN_DEFAULT_PASSWORD,
            "new_password": ADMIN_PASSWORD_AFTER_CHANGE,
        },
        headers=auth(token),
    )
    assert r.status_code == 204, r.text
    return token


_counter = {"n": 0}


def unique_email() -> str:
    _counter["n"] += 1
    return f"user{_counter['n']}@test.local"


def create_user(client, admin_token: str, role: str = "member", email: str | None = None):
    """Create a user via the API and return (user_dict, token)."""
    email = email or unique_email()
    r = client.post(
        "/api/members",
        json={"name": "Test User", "email": email, "password": "password123", "role": role},
        headers=auth(admin_token),
    )
    assert r.status_code == 201, r.text
    login = client.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert login.status_code == 200, login.text
    return r.json(), login.json()["token"]
