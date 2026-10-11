"""GET /api/items/suggest — the suggestion engine for the intake flow.

Pins: prefix-sibling ranking (longest significant shared prefix first,
indicator-stripped, padding never creates siblings), keyword parity with
?q= (wildcard escaping), semantic search — and its degradation to [] on
any embedding/extension unavailability (the endpoint NEVER answers 503).
"""

from tests.conftest import auth, create_user


def _create_item(client, token, sku, name=None, *, barcode=None):
    body = {"sku": sku, "name": name or f"Item {sku}", "price_cents": 100}
    if barcode:
        body["barcode"] = barcode
    r = client.post("/api/items", json=body, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _suggest(client, token, **params):
    r = client.get("/api/items/suggest", params=params, headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


# --- by_prefix -----------------------------------------------------------------


def test_by_prefix_ranks_longest_shared_first(client, admin_token):
    user, token = create_user(client, admin_token)
    near = _create_item(client, admin_token, "NEAR-1", "Near sibling",
                        barcode="5901234999991")     # shares 5901234 (7)
    far = _create_item(client, admin_token, "FAR-1", "Far sibling",
                       barcode="5901999999991")      # shares 5901 (4) -> below floor
    mid = _create_item(client, admin_token, "MID-1", "Mid sibling",
                       barcode="5901230000004")      # shares 590123 (6) -> at floor

    result = _suggest(client, token, barcode="5901234123457")
    ranked = [i["id"] for i in result["by_prefix"]]
    assert ranked.index(near["id"]) < ranked.index(mid["id"])
    assert far["id"] not in ranked
    assert result["by_keyword"] == []  # no q= given


def test_by_prefix_case_code_ranks_with_its_retail_gtin13(client, admin_token):
    # Indicator-stripped comparison: a GTIN-14 case pack is the closest
    # sibling of the retail GTIN-13 of the same product family.
    user, token = create_user(client, admin_token)
    retail = _create_item(client, admin_token, "RETAIL-1", "Retail unit",
                          barcode="5901234123457")
    result = _suggest(client, token, barcode="15901234123454")  # indicator 1
    assert [i["id"] for i in result["by_prefix"]] == [retail["id"]]


def test_by_prefix_padding_never_creates_siblings(client, admin_token):
    user, token = create_user(client, admin_token)
    _create_item(client, admin_token, "E8-A", "First EAN-8",
                 barcode="12345670")   # canonical 00000012345670
    result = _suggest(client, token, barcode="87654325")  # another EAN-8
    assert result["by_prefix"] == []


def test_by_prefix_requires_a_valid_gtin(client, admin_token):
    user, token = create_user(client, admin_token)
    r = client.get("/api/items/suggest", params={"barcode": "5901234123458"},
                   headers=auth(token))
    assert r.status_code == 422, r.text  # bad check digit — same rule as ?barcode=


# --- by_keyword ------------------------------------------------------------------


def test_by_keyword_matches_and_escapes_wildcards(client, admin_token):
    user, token = create_user(client, admin_token)
    odd = _create_item(client, admin_token, "ODD-1", "Cafe_ 50% blend")
    _create_item(client, admin_token, "PLAIN-1", "Plain product")

    result = _suggest(client, token, q="50%")
    assert [i["id"] for i in result["by_keyword"]] == [odd["id"]]
    # A bare wildcard must not match everything (escaped ILIKE, ?q= parity):
    # q="%" finds only the items whose text contains a LITERAL percent sign
    # (some seed descriptions do, e.g. "65% mechanical keyboard") — never
    # the whole catalogue, and never the item without one.
    result = _suggest(client, token, q="%")
    plain = [i for i in result["by_keyword"] if i["sku"] == "PLAIN-1"]
    assert plain == []
    assert len(result["by_keyword"]) < 15  # not everything
    # A bare underscore likewise: literal-only (unescaped, "_" would match
    # any single character, i.e. everything).
    result = _suggest(client, token, q="_")
    assert [i["id"] for i in result["by_keyword"]] == [odd["id"]]  # "Cafe_ 50% blend"


# --- by_semantic --------------------------------------------------------------------


def test_by_semantic_returns_results_with_embeddings(client, admin_token):
    # conftest fakes embeddings as deterministic one-hot vectors of the
    # text hash, so a query equal to the item's embedding text is an exact
    # match — the positive pin for the semantic group.
    from app.embeddings import item_embedding_text

    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "WARM-1", "Thermos flask")
    query = item_embedding_text("Thermos flask", "")

    result = _suggest(client, token, q=query)
    # The exact-text match ranks FIRST (the one-hot fakes tie everything
    # else at distance 1.0; the vector scan returns the top-5 — ranked).
    assert result["by_semantic"][0]["id"] == item["id"]


def test_by_semantic_degrades_to_empty_never_503(client, admin_token, monkeypatch):
    """Decision 7 pin: any embedding/extension unavailability is a
    suggestion MISS ([]), never a failure of the endpoint."""
    user, token = create_user(client, admin_token)
    _create_item(client, admin_token, "WARM-2", "Thermos flask")

    import app.routers.items as items_router
    monkeypatch.setattr(items_router, "embed_text", lambda q: None)

    result = _suggest(client, token, q="thermos")
    assert result["by_semantic"] == []
    # The other groups still work — the flow is not failed by the degrade.
    assert isinstance(result["by_keyword"], list)


# --- gates --------------------------------------------------------------------------


def test_unauthenticated_is_401(client):
    r = client.get("/api/items/suggest", params={"q": "x"})
    assert r.status_code == 401


def test_member_can_suggest(client, admin_token):
    # The intake form is the MEMBER path: any authenticated role.
    user, token = create_user(client, admin_token)
    result = _suggest(client, token, q="anything")
    assert set(result.keys()) == {"by_prefix", "by_keyword", "by_semantic"}


def test_limit_bounds(client, admin_token):
    user, token = create_user(client, admin_token)
    r = client.get("/api/items/suggest", params={"q": "x", "limit": 26},
                   headers=auth(token))
    assert r.status_code == 422
    r = client.get("/api/items/suggest", params={"q": "x", "limit": 0},
                   headers=auth(token))
    assert r.status_code == 422
