"""POST /api/cart/scan — scan-to-cart over the shared decision table.

Pins: the gates (401/403), the badge invariant (badges never reach the
catalogue), the misread 422 (never filed, never suggested around), exact
identity in both forms (canonical GTIN incl. UPC-E→EAN-13, exact SKU), the
structured 404 that drives the Task-5 intake modal (analysis + catalogable +
suggestions in one round-trip), stock-untouched, per-user isolation, qty.
"""

from tests.conftest import auth, create_user, unique_email


def _create_item(client, token, sku, *, price_cents=100, stock=5, barcode=None):
    body = {"sku": sku, "name": f"Item {sku}", "price_cents": price_cents,
            "stock": stock}
    if barcode:
        body["barcode"] = barcode
    r = client.post("/api/items", json=body, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _scan(client, token, code, symbology="", qty=1):
    return client.post(
        "/api/cart/scan",
        json={"code": code, "symbology": symbology, "qty": qty},
        headers=auth(token),
    )


def _cart(client, token):
    r = client.get("/api/cart", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _flagged_token(client, admin_token):
    """A token whose account still has must_change_password set."""
    from tests.test_cart import _flagged_token as helper
    return helper(client, admin_token)


BADGE = "HCRM1:" + "A" * 43


# --- gates -------------------------------------------------------------------


def test_unauthenticated_is_401(client):
    assert _scan(client, None or "", "8412345678905").status_code == 401


def test_must_change_password_is_403(client, admin_token):
    token = _flagged_token(client, admin_token)
    assert _scan(client, token, "8412345678905").status_code == 403


# --- the badge invariant ------------------------------------------------------


def test_badge_payload_is_400_and_cart_untouched(client, admin_token):
    user, token = create_user(client, admin_token)
    r = _scan(client, token, BADGE)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["status"] == "badge"
    assert "qr-login" in r.json()["detail"]["message"]
    assert _cart(client, token)["lines"] == []


def test_raw_43_char_token_is_just_an_opaque_miss(client, admin_token):
    # The HCRM1: prefix is the badge contract; without it a 43-char token is
    # an ordinary opaque label (a miss, not an explosion).
    user, token = create_user(client, admin_token)
    r = _scan(client, token, "A" * 43)
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["analysis"]["kind"] == "opaque"


# --- exact identity: hit ------------------------------------------------------


def test_known_gtin_adds_to_cart(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "BTL-001", price_cents=250,
                        barcode="8412345678905")

    r = _scan(client, token, "8412345678905")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "added"
    assert body["match"] == "barcode"
    assert body["item"]["id"] == item["id"]
    assert body["qty"] == 1
    # The full cart rides along (Task-1 mutator contract).
    assert body["cart"]["lines"][0]["item"]["id"] == item["id"]
    assert body["cart"]["total_cents"] == 250
    assert body["cart"] == _cart(client, token)


def test_rescan_increments_never_409(client, admin_token):
    user, token = create_user(client, admin_token)
    _create_item(client, admin_token, "BTL-002", barcode="8412345678905")
    _scan(client, token, "8412345678905")
    r = _scan(client, token, "8412345678905", qty=3)
    assert r.status_code == 200, r.text
    assert r.json()["qty"] == 4
    assert len(r.json()["cart"]["lines"]) == 1


def test_upce_scan_finds_the_item_filed_under_its_upca_gtin(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "TIN-001",
                        barcode="042100005264")  # canonical 00042100005264
    r = _scan(client, token, "425261", symbology="UPC-E")
    assert r.status_code == 200, r.text
    assert r.json()["match"] == "barcode"
    assert r.json()["item"]["id"] == item["id"]


def test_opaque_label_matches_exact_sku_case_insensitively(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "MYLABEL-42")
    for code in ("mylabel-42", "MYLABEL-42", "MyLabel-42"):
        r = _scan(client, token, code)
        assert r.status_code == 200, (code, r.text)
        assert r.json()["match"] == "sku"
        assert r.json()["item"]["id"] == item["id"]


def test_stock_is_never_mutated(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "STOCK-1", stock=3)
    r = _scan(client, token, item["sku"], qty=10)  # qty > stock is allowed
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/items/{item['id']}",
                        headers=auth(admin_token)).json()
    assert detail["stock"] == 3


def test_adds_land_only_in_the_callers_cart(client, admin_token):
    alice, alice_token = create_user(client, admin_token)
    bob, bob_token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ISO-1", barcode="8412345678905")
    _scan(client, alice_token, "8412345678905")
    assert _cart(client, bob_token)["lines"] == []
    assert _cart(client, alice_token)["total_qty"] == 1


# --- the no-guess outcomes ------------------------------------------------------


def test_misread_is_422_with_no_suggestions(client, admin_token):
    user, token = create_user(client, admin_token)
    r = _scan(client, token, "8412345678906")  # check digit +1
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "misread"
    assert "MISREAD" in detail["message"]
    assert detail["analysis"]["kind"] == "invalid-gtin"
    assert "suggestions" not in detail  # never suggested around
    assert _cart(client, token)["lines"] == []


def test_unknown_gtin_404_drives_the_intake_modal(client, admin_token):
    user, token = create_user(client, admin_token)
    # A same-company sibling exists (shares the scanned code's GS1 company
    # prefix 5901234…): it is the suggestion; an unrelated-barcode item
    # exists too: it must NOT be suggested.
    sibling = _create_item(client, admin_token, "SIB-1",
                           barcode="5901234999991")
    _create_item(client, admin_token, "UNREL-1", barcode="4006381333931")

    r = _scan(client, token, "5901234123457")  # valid, absent (Poland)
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "unknown"
    assert "new-entry candidate" in detail["message"]
    assert detail["catalogable"] is True
    analysis = detail["analysis"]
    assert analysis["kind"] == "gtin"
    assert analysis["gtin14"] == "05901234123457"
    assert analysis["region"] == "Poland"
    assert analysis["usage"] == "retail"
    # Prefix siblings: the same-company item, never the unrelated one.
    suggested_ids = [i["id"] for i in detail["suggestions"]["by_prefix"]]
    assert sibling["id"] in suggested_ids
    # 4006381333931's significant view (4006381333931) shares nothing with
    # 5901234123457 — the anti-guess pin.
    assert all(i["barcode"] != "04006381333931" for i in detail["suggestions"]["by_prefix"])
    assert _cart(client, token)["lines"] == []  # nothing auto-added


def test_unknown_opaque_404_with_upce_hint(client, admin_token):
    user, token = create_user(client, admin_token)
    r = _scan(client, token, "425261")  # bare 6 digits, no symbology
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "unknown"
    assert detail["catalogable"] is False
    assert detail["analysis"]["kind"] == "opaque"
    # The verbatim bridge hint: the scanner must transmit its symbology ID.
    assert "UPC-E" in detail["message"]


def test_unknown_opaque_keyword_suggestion(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "LABEL-ABC-99")
    r = _scan(client, token, "label-abc")  # SKU fragment, not an exact match
    assert r.status_code == 404, r.text
    suggested = r.json()["detail"]["suggestions"]["by_keyword"]
    assert item["id"] in [i["id"] for i in suggested]


def test_unknown_scan_404_never_embeds(client, admin_token, monkeypatch):
    """The 404 surfaces by_prefix/by_keyword only: computing the discarded
    semantic group would run a real embedding forward pass on every unknown
    scan — pinned off via suggest_items(semantic=False)."""
    import app.routers.items as items_router

    calls = []
    real = items_router.embed_text
    monkeypatch.setattr(items_router, "embed_text",
                        lambda q: calls.append(q) or real(q))

    user, token = create_user(client, admin_token)
    r = _scan(client, token, "5901234123457")  # valid, absent GTIN
    assert r.status_code == 404, r.text
    assert calls == []
    r = _scan(client, token, "some-opaque-label")  # opaque miss, q >= 2 chars
    assert r.status_code == 404, r.text
    assert calls == []


# --- validation -----------------------------------------------------------------


def test_bad_input_is_422(client, admin_token):
    user, token = create_user(client, admin_token)
    for body in ({"code": ""}, {"code": "x" * 65}, {"code": "123", "qty": 0},
                 {"code": "123", "qty": 10_001}, {"code": "123", "extra": 1}):
        r = client.post("/api/cart/scan", json=body, headers=auth(token))
        assert r.status_code == 422, (body, r.text)
