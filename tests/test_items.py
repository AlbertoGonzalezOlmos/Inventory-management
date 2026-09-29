"""Tests for catalogue endpoints and vector search."""

import app.routers.items as items_router
from app.embeddings import EMBEDDING_DIM
from tests.conftest import auth, create_user


def test_healthz(client):
    r = client.get("/api/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Cached at startup / first call — never re-probed under load.
    assert body["vector"] is True
    # The suite never loads the real model (warm_up is neutralised).
    assert body["embeddings"] == "not_loaded"


def test_items_require_auth(client):
    assert client.get("/api/items").status_code == 401
    assert client.get("/api/items/vector-search?q=test").status_code == 401


def test_list_and_keyword_search(client, admin_token):
    r = client.get("/api/items", headers=auth(admin_token))
    assert r.status_code == 200
    assert len(r.json()) >= 12
    assert all("has_embedding" in i for i in r.json())

    r = client.get("/api/items?q=mouse", headers=auth(admin_token))
    assert any("Mouse" in i["name"] for i in r.json())


def test_create_item_auto_embeds(client, admin_token):
    r = client.post(
        "/api/items",
        json={"sku": "T-EMB-1", "name": "Test Widget", "description": "a widget", "price_cents": 100},
        headers=auth(admin_token),
    )
    assert r.status_code == 201
    assert r.json()["has_embedding"] is True
    client.delete(f"/api/items/{r.json()['id']}", headers=auth(admin_token))


def test_duplicate_sku_conflict(client, admin_token):
    body = {"sku": "EX-001", "name": "Duplicate", "price_cents": 1}
    r = client.post("/api/items", json=body, headers=auth(admin_token))
    assert r.status_code == 409


def test_validation_guards(client, admin_token):
    r = client.post(
        "/api/items", json={"sku": "T-NEG", "name": "X", "price_cents": -5}, headers=auth(admin_token)
    )
    assert r.status_code == 422
    r = client.post(
        "/api/items",
        json={"sku": "T-LONG", "name": "N" * 300, "price_cents": 1},
        headers=auth(admin_token),
    )
    assert r.status_code == 422


def test_member_cannot_modify_catalogue(client, admin_token):
    _, member_token = create_user(client, admin_token, role="member")
    r = client.post(
        "/api/items", json={"sku": "T-M", "name": "X", "price_cents": 1}, headers=auth(member_token)
    )
    assert r.status_code == 403
    assert client.delete("/api/items/1", headers=auth(member_token)).status_code == 403


def test_vector_search_embeds_query_exactly_once(client, admin_token, monkeypatch):
    """Regression: the query used to be embedded twice per search."""
    calls = {"n": 0}

    def counting_embed(text):
        calls["n"] += 1
        vec = [0.0] * EMBEDDING_DIM
        vec[0] = 1.0
        return vec

    monkeypatch.setattr(items_router, "embed_text", counting_embed)
    r = client.get("/api/items/vector-search?q=anything", headers=auth(admin_token))
    assert r.status_code == 200
    assert calls["n"] == 1


def test_vector_search_category_filter(client, admin_token):
    r = client.get(
        "/api/items/vector-search?q=something&category=Kitchen", headers=auth(admin_token)
    )
    assert r.status_code == 200
    results = r.json()
    assert len(results) > 0
    assert all(i["category"] == "Kitchen" for i in results)

    # Pagination still works with the filter applied in SQL.
    r = client.get(
        "/api/items/vector-search?q=something&category=Kitchen&limit=2",
        headers=auth(admin_token),
    )
    assert len(r.json()) == 2
    assert all(i["category"] == "Kitchen" for i in r.json())


def test_keyword_search_treats_wildcards_literally(client, admin_token):
    """Regression: q=% or q=_ used to match every row (unescaped LIKE wildcards)."""
    # "%" must match literally (one seeded description contains "65%"),
    # not act as a wildcard returning the whole catalogue.
    r = client.get("/api/items", params={"q": "%"}, headers=auth(admin_token))
    assert r.status_code == 200
    items = r.json()
    assert 0 < len(items) < 12
    assert all("%" in (i["name"] + i["description"] + i["sku"]) for i in items)

    r = client.get("/api/items", params={"q": "___"}, headers=auth(admin_token))
    assert r.status_code == 200
    assert all("_" in (i["name"] + i["description"] + i["sku"]) for i in r.json())

    # Ordinary substring search still works.
    r = client.get("/api/items", params={"q": "Mouse"}, headers=auth(admin_token))
    assert any("Mouse" in i["name"] for i in r.json())


def test_patch_preserves_embedding_when_model_unavailable(client, admin_token, monkeypatch):
    """Regression: a model outage used to null out the item's embedding (data loss)."""
    r = client.post(
        "/api/items",
        json={"sku": "T-OUT", "name": "Outage Widget", "description": "orig", "price_cents": 1},
        headers=auth(admin_token),
    )
    assert r.status_code == 201 and r.json()["has_embedding"] is True
    item_id = r.json()["id"]

    # Simulate the embedding model being down.
    monkeypatch.setattr(items_router, "embed_text_blob", lambda text: None)

    # Text changes are rejected with 503 and nothing is persisted.
    r = client.patch(
        f"/api/items/{item_id}", json={"description": "new text"}, headers=auth(admin_token)
    )
    assert r.status_code == 503
    after = client.get(f"/api/items/{item_id}", headers=auth(admin_token)).json()
    assert after["has_embedding"] is True
    assert after["description"] == "orig"

    # Non-text changes still work during an outage.
    r = client.patch(f"/api/items/{item_id}", json={"price_cents": 42}, headers=auth(admin_token))
    assert r.status_code == 200
    assert r.json()["price_cents"] == 42
    assert r.json()["has_embedding"] is True

    client.delete(f"/api/items/{item_id}", headers=auth(admin_token))


def test_patch_null_fields_rejected(client, admin_token):
    """Explicit null in a PATCH body must be a 422, never a 500 from a
    NOT NULL constraint at commit time."""
    item = client.get("/api/items", headers=auth(admin_token)).json()[0]
    for field in ("name", "description", "category", "price_cents", "stock", "image_url"):
        r = client.patch(f"/api/items/{item['id']}", json={field: None}, headers=auth(admin_token))
        assert r.status_code == 422, (field, r.status_code)

    from tests.conftest import create_user
    member, _ = create_user(client, admin_token)
    for field in ("name", "role", "password"):
        r = client.patch(f"/api/members/{member['id']}", json={field: None}, headers=auth(admin_token))
        assert r.status_code == 422, (field, r.status_code)
    # Nothing was persisted by the failed patches.
    assert client.get(f"/api/items/{item['id']}", headers=auth(admin_token)).json()["name"] == item["name"]


def test_unknown_fields_rejected(client, admin_token):
    """Typo'd/unknown fields must not be silently ignored behind a 2xx."""
    # Typo on create
    r = client.post(
        "/api/items",
        json={"sku": "T-TYPO", "name": "X", "price_cent": 5},
        headers=auth(admin_token),
    )
    assert r.status_code == 422
    # sku is immutable: not part of ItemPatch
    item = client.get("/api/items", headers=auth(admin_token)).json()[0]
    r = client.patch(f"/api/items/{item['id']}", json={"sku": "HACKED"}, headers=auth(admin_token))
    assert r.status_code == 422
    # Unknown role-like field on member creation
    r = client.post(
        "/api/members",
        json={"name": "X", "email": "typo@t.local", "password": "password123", "role2": "admin"},
        headers=auth(admin_token),
    )
    assert r.status_code == 422
    # Oversized login input
    r = client.post("/api/auth/login", json={"email": "x" * 300, "password": "y"})
    assert r.status_code == 422


def test_post_item_model_unavailable_503(client, admin_token, monkeypatch):
    """With the embedding model down, POST must not silently create an item
    that is invisible to semantic search (same policy as PATCH)."""
    monkeypatch.setattr(items_router, "embed_text_blob", lambda t: None)
    r = client.post(
        "/api/items",
        json={"sku": "T-DOWN", "name": "Model Down", "price_cents": 1},
        headers=auth(admin_token),
    )
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"].lower()
    # Nothing was persisted.
    names = [i["sku"] for i in client.get("/api/items?limit=100", headers=auth(admin_token)).json()]
    assert "T-DOWN" not in names


def test_vector_search_similarity_clamped(client, admin_token):
    r = client.get("/api/items/vector-search?q=zzzq", headers=auth(admin_token))
    assert r.status_code == 200
    assert all(0.0 <= i["similarity"] <= 1.0 for i in r.json())
