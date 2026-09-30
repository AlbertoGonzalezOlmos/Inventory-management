"""Tests for the catalogue barcode field: normalisation, uniqueness, lookup,
and the schema migration that adds the column to pre-existing databases."""

import sqlite3

import pytest

from app.barcodes import normalize_gtin
from tests.conftest import auth, create_user

# A valid Spanish EAN-13 (841 = Spain & Andorra) and its canonical GTIN-14.
EAN13 = "8412345678905"
GTIN14 = "08412345678905"


def _create(client, token, sku, **extra):
    body = {"sku": sku, "name": f"Item {sku}", "price_cents": 100}
    body.update(extra)
    return client.post("/api/items", json=body, headers=auth(token))


def test_create_stores_canonical_gtin14(client, admin_token):
    r = _create(client, admin_token, "BC-1", barcode=EAN13)
    assert r.status_code == 201, r.text
    assert r.json()["barcode"] == GTIN14


def test_create_accepts_other_gtin_forms(client, admin_token):
    # EAN-8, UPC-A and GTIN-14 all land in the same canonical space.
    r = _create(client, admin_token, "BC-E8", barcode="96385074")
    assert r.status_code == 201 and r.json()["barcode"] == "00000096385074"
    r = _create(client, admin_token, "BC-UPC", barcode="036000291452")
    assert r.status_code == 201 and r.json()["barcode"] == "00036000291452"
    r = _create(client, admin_token, "BC-14", barcode="14006381333938")
    assert r.status_code == 201 and r.json()["barcode"] == "14006381333938"


def test_create_rejects_invalid_check_digit(client, admin_token):
    r = _create(client, admin_token, "BC-BAD", barcode="8412345678906")
    assert r.status_code == 422
    assert "valid GTIN" in r.text


def test_create_rejects_non_gtin_barcode(client, admin_token):
    # Internal SKU strings are not GTINs; the sku field already covers them.
    r = _create(client, admin_token, "BC-SKU", barcode="EX-001")
    assert r.status_code == 422


def test_create_empty_barcode_is_null(client, admin_token):
    r = _create(client, admin_token, "BC-EMPTY", barcode="")
    assert r.status_code == 201 and r.json()["barcode"] is None
    r = _create(client, admin_token, "BC-OMIT")
    assert r.status_code == 201 and r.json()["barcode"] is None


def test_duplicate_barcode_conflicts(client, admin_token):
    assert _create(client, admin_token, "BC-DUP-A", barcode=EAN13).status_code == 201
    r = _create(client, admin_token, "BC-DUP-B", barcode=EAN13)
    assert r.status_code == 409
    assert "BC-DUP-A" in r.json()["detail"]


def test_duplicate_detection_survives_format_changes(client, admin_token):
    # Filed as EAN-13; a second entry attempted with the padded GTIN-14 form.
    assert _create(client, admin_token, "BC-FMT-A", barcode=EAN13).status_code == 201
    r = _create(client, admin_token, "BC-FMT-B", barcode=GTIN14)
    assert r.status_code == 409


def test_lookup_by_barcode_any_form(client, admin_token):
    _create(client, admin_token, "BC-LOOK", barcode=EAN13)
    for form in (EAN13, GTIN14, f" {EAN13} "):
        r = client.get("/api/items", params={"barcode": form},
                       headers=auth(admin_token))
        assert r.status_code == 200, form
        assert [i["sku"] for i in r.json()] == ["BC-LOOK"], form


def test_lookup_by_barcode_invalid_is_422(client, admin_token):
    r = client.get("/api/items", params={"barcode": "8412345678906"},
                   headers=auth(admin_token))
    assert r.status_code == 422  # misread — must not masquerade as "not found"


def test_keyword_search_matches_barcode(client, admin_token):
    # The HID-keyboard workflow: a scanner "types" the digits into the SPA
    # search box; the q filter must find the item without the barcode param.
    _create(client, admin_token, "BC-Q", barcode=EAN13)
    r = client.get("/api/items", params={"q": EAN13}, headers=auth(admin_token))
    assert r.status_code == 200
    assert any(i["sku"] == "BC-Q" for i in r.json())


def test_patch_barcode_roundtrip(client, admin_token):
    item_id = _create(client, admin_token, "BC-PATCH", barcode=EAN13).json()["id"]
    # Replace with another valid GTIN (German EAN-13):
    r = client.patch(f"/api/items/{item_id}",
                     json={"barcode": "4006381333931"}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["barcode"] == "04006381333931"
    # Clear it with "":
    r = client.patch(f"/api/items/{item_id}", json={"barcode": ""},
                     headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["barcode"] is None
    # Explicit null stays rejected (repo-wide PATCH policy, conftest _PatchIn):
    r = client.patch(f"/api/items/{item_id}", json={"barcode": None},
                     headers=auth(admin_token))
    assert r.status_code == 422


def test_patch_barcode_conflict(client, admin_token):
    _create(client, admin_token, "BC-OWN", barcode=EAN13)
    other = _create(client, admin_token, "BC-OTHER").json()["id"]
    r = client.patch(f"/api/items/{other}", json={"barcode": EAN13},
                     headers=auth(admin_token))
    assert r.status_code == 409 and "BC-OWN" in r.json()["detail"]


def test_staff_role_can_set_barcodes(client, admin_token):
    _staff, staff_token = create_user(client, admin_token, role="staff")
    r = _create(client, staff_token, "BC-STAFF", barcode=EAN13)
    assert r.status_code == 201 and r.json()["barcode"] == GTIN14


def test_migration_adds_barcode_to_legacy_db(tmp_path, monkeypatch):
    """A pre-barcode database (as shipped before this feature) must upgrade
    on boot: column added, unique index created, existing rows untouched."""
    import sqlalchemy

    import app.main

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    # A W4.0-era database: users already has must_change_password, items does
    # not have barcode yet — exactly the state this migration must upgrade.
    conn.execute(
        "CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY, email VARCHAR "
        "NOT NULL, name VARCHAR NOT NULL, password_hash VARCHAR NOT NULL, "
        "role VARCHAR NOT NULL, must_change_password BOOLEAN NOT NULL DEFAULT "
        "0, created_at DATETIME NOT NULL)"
    )
    # The old items schema, verbatim minus the new column:
    conn.execute(
        "CREATE TABLE items (id INTEGER NOT NULL PRIMARY KEY, sku VARCHAR NOT "
        "NULL, name VARCHAR NOT NULL, description VARCHAR NOT NULL, category "
        "VARCHAR NOT NULL, price_cents INTEGER NOT NULL, stock INTEGER NOT "
        "NULL, image_url VARCHAR NOT NULL, embedding BLOB, created_at "
        "DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
    )
    conn.execute(
        "INSERT INTO items VALUES (1, 'OLD-1', 'Legacy item', '', 'general', "
        "0, 0, '', NULL, '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    legacy_engine = sqlalchemy.create_engine(f"sqlite:///{legacy}")
    monkeypatch.setattr(app.main, "engine", legacy_engine)
    app.main._migrate_schema()

    conn = sqlite3.connect(legacy)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    assert "barcode" in cols
    assert conn.execute("SELECT barcode FROM items WHERE id=1").fetchone()[0] is None
    # The unique index exists and bites:
    conn.execute("UPDATE items SET barcode=? WHERE id=1", (GTIN14,))
    conn.execute(
        "INSERT INTO items (id, sku, name, description, category, "
        "price_cents, stock, image_url, embedding, created_at, updated_at) "
        "VALUES (2, 'OLD-2', 'x', '', 'general', 0, 0, '', NULL, "
        "'2026-01-01', '2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE items SET barcode=? WHERE id=2", (GTIN14,))
    conn.close()
    legacy_engine.dispose()
