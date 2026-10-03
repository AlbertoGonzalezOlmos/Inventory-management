"""The schema-migration table (app/main.py COLUMN_MIGRATIONS / INDEX_MIGRATIONS).

`create_all()` adds tables it does not find but never ALTERs one it does, so
every new column on an existing table has to be migrated by hand. Three feature
branches each added an `if` block at the same anchor inside `_migrate_schema()`
and collided textually every time — round-5's `auth_tokens` index, PR #2's
`items.barcode`, the M-10 branch's `users.qr_badge_hash`. The table exists so
that adding a migration is an *append*, which merges, rather than an edit to a
shared block, which does not.

These tests pin the two properties that make the append safe: the table is
well-formed (no duplicate or malformed entry can silently double-ALTER or
inject SQL), and applying it is idempotent.
"""

import re
import sqlite3

import pytest
import sqlalchemy

import app.main
from app.main import COLUMN_MIGRATIONS, INDEX_MIGRATIONS


def _legacy_db(path, *, with_must_change=True):
    """A database from before the scanner work: no barcode, no badge column."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY, email VARCHAR "
        "NOT NULL, name VARCHAR NOT NULL, password_hash VARCHAR NOT NULL, "
        "role VARCHAR NOT NULL, "
        + ("must_change_password BOOLEAN NOT NULL DEFAULT 0, " if with_must_change else "")
        + "created_at DATETIME NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE items (id INTEGER NOT NULL PRIMARY KEY, sku VARCHAR NOT "
        "NULL, name VARCHAR NOT NULL, description VARCHAR NOT NULL, category "
        "VARCHAR NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT "
        "NULL, image_url VARCHAR NOT NULL, embedding BLOB, created_at "
        "DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE auth_tokens (token VARCHAR NOT NULL PRIMARY KEY, "
        "user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL)"
    )
    conn.execute(
        "INSERT INTO items VALUES (1, 'OLD-1', 'Legacy item', '', 'general', "
        "0, 0, '', NULL, '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()


def _run(path, monkeypatch, times=1):
    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(app.main, "engine", engine)
    for _ in range(times):
        app.main._migrate_schema()
    engine.dispose()


def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _indexes(path, table):
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


# --- the table itself ---------------------------------------------------------


def test_migration_table_is_well_formed():
    """No duplicates, no malformed identifiers, no empty DDL.

    A duplicate (table, column) would be harmless only by accident (the second
    entry sees the column and skips), and a malformed identifier would end up
    interpolated into SQL — so both are refused here rather than at boot on
    somebody's database.
    """
    identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    seen = set()
    for table, column, ddl, extra in COLUMN_MIGRATIONS:
        assert identifier.fullmatch(table), table
        assert identifier.fullmatch(column), column
        assert ddl.strip(), f"{table}.{column} has no column DDL"
        assert (table, column) not in seen, f"duplicate migration for {table}.{column}"
        seen.add((table, column))
        for statement in extra:
            assert statement.strip().upper().startswith(("CREATE ", "ALTER ")), statement
    for table, statement in INDEX_MIGRATIONS:
        assert identifier.fullmatch(table), table
        assert "IF NOT EXISTS" in statement.upper(), (
            f"an index migration must be idempotent, it runs on every boot: {statement}")


def test_every_migration_the_app_currently_needs_is_listed():
    """The table is the whole story: three columns, one index."""
    assert {(t, c) for t, c, _d, _e in COLUMN_MIGRATIONS} == {
        ("users", "must_change_password"),
        ("items", "barcode"),
        ("users", "qr_badge_hash"),
    }
    assert {t for t, _s in INDEX_MIGRATIONS} == {"auth_tokens"}


def test_a_malformed_entry_fails_loudly(tmp_path, monkeypatch):
    """Prove the guard works before trusting it (this repo's rule for gates)."""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setattr(app.main, "COLUMN_MIGRATIONS",
                        (("users", "bad; DROP TABLE users--", "TEXT", ()),))
    engine = sqlalchemy.create_engine(f"sqlite:///{db}")
    monkeypatch.setattr(app.main, "engine", engine)
    with pytest.raises(ValueError, match="invalid column"):
        app.main._migrate_schema()
    engine.dispose()
    # Nothing was executed: the table is intact and no column was added.
    assert _columns(db, "users") == {
        "id", "email", "name", "password_hash", "role",
        "must_change_password", "created_at"}


# --- applying it --------------------------------------------------------------


def test_all_migrations_land_on_a_pre_scanner_database(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    _run(db, monkeypatch)
    assert {"barcode"} <= _columns(db, "items")
    assert {"qr_badge_hash"} <= _columns(db, "users")
    assert "ix_items_barcode" in _indexes(db, "items")
    assert "ix_users_qr_badge_hash" in _indexes(db, "users")
    assert "ix_auth_tokens_expires_at" in _indexes(db, "auth_tokens")
    # Existing rows survive, with the new column NULL:
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT barcode FROM items WHERE id=1").fetchone()[0] is None
    conn.close()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Booting twice (or twice in one process) must not error or duplicate."""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    _run(db, monkeypatch, times=3)
    assert "barcode" in _columns(db, "items")
    assert "qr_badge_hash" in _columns(db, "users")
    assert "ix_items_barcode" in _indexes(db, "items")


def test_a_database_from_before_w40_also_upgrades(tmp_path, monkeypatch):
    """No must_change_password either: the oldest schema this repo shipped."""
    db = tmp_path / "ancient.db"
    _legacy_db(db, with_must_change=False)
    _run(db, monkeypatch)
    assert {"must_change_password", "qr_badge_hash"} <= _columns(db, "users")


def test_missing_tables_are_skipped_not_fatal(tmp_path, monkeypatch):
    """A database with none of the tables (fresh install path) must not raise."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    _run(db, monkeypatch)
    assert _columns(db, "unrelated") == {"x"}
