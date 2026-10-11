"""Member scan requests: the unknown-item queue (staff-only resolution).

Pins the lifecycle end-to-end: filing (with server-side re-validation —
badge 400, misread 422, already-catalogued 409, duplicate-open 409), /mine,
the staff queue, resolve (link + optional add_to_cart via the race-free
upsert; stock untouched), dismiss, non-open 409s, member deletion cleanup,
and the weak-ref tolerance (a resolved request survives item deletion).
"""

from sqlmodel import Session, select

from app.database import engine
from app.models import ScanRequest
from tests.conftest import auth, create_user, unique_email

BADGE = "HCRM1:" + "A" * 43


def _file(client, token, code, name="Proposed thing", price_cents=500,
          symbology=""):
    return client.post(
        "/api/scan-requests",
        json={"code": code, "symbology": symbology, "name": name,
              "price_cents": price_cents},
        headers=auth(token),
    )


def _create_item(client, token, sku, barcode=None):
    body = {"sku": sku, "name": f"Item {sku}", "price_cents": 100}
    if barcode:
        body["barcode"] = barcode
    r = client.post("/api/items", json=body, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


# --- filing ---------------------------------------------------------------------


def test_member_files_an_unknown_gtin(client, admin_token):
    member, member_token = create_user(client, admin_token)
    r = _file(client, member_token, "5901234123457", name="Polish biscuit",
              price_cents=350)
    assert r.status_code == 201, r.text
    body = r.json()
    # The classification is server-derived (never trusted from the client).
    assert body["kind"] == "gtin"
    assert body["gtin14"] == "05901234123457"
    assert body["status"] == "open"
    assert body["requester"]["email"] == member["email"]
    assert body["name"] == "Polish biscuit"
    assert body["price_cents"] == 350


def test_opaque_code_files_with_raw_identity(client, admin_token):
    member, member_token = create_user(client, admin_token)
    r = _file(client, member_token, "supplier-label-9")
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "opaque"
    assert r.json()["gtin14"] is None


def test_misread_is_never_filed(client, admin_token):
    member, member_token = create_user(client, admin_token)
    r = _file(client, member_token, "8412345678906")  # check digit +1
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["status"] == "misread"


def test_badge_payload_is_400(client, admin_token):
    member, member_token = create_user(client, admin_token)
    r = _file(client, member_token, BADGE)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["status"] == "badge"


def test_already_catalogued_code_is_409_with_the_item(client, admin_token):
    member, member_token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "KNOWN-1",
                        barcode="5901234123457")
    r = _file(client, member_token, "5901234123457")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "already"
    assert detail["item"]["id"] == item["id"]


def test_duplicate_open_request_is_409_with_the_request(client, admin_token):
    member, member_token = create_user(client, admin_token)
    first = _file(client, member_token, "5901234123457").json()
    r = _file(client, member_token, "5901234123457")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "duplicate"
    assert detail["request"]["id"] == first["id"]

    # A different user, same code: a separate request (no cross-user dedup)…
    other, other_token = create_user(client, admin_token)
    assert _file(client, other_token, "5901234123457").status_code == 201
    # …and a dismissed request does not block re-filing.
    client.post(f"/api/scan-requests/{first['id']}/dismiss",
                headers=auth(admin_token))
    assert _file(client, member_token, "5901234123457").status_code == 201


def test_unauthenticated_cannot_file(client):
    assert _file(client, None or "", "5901234123457").status_code == 401


# --- /mine and the staff queue ------------------------------------------------------


def test_mine_lists_own_newest_first(client, admin_token):
    member, member_token = create_user(client, admin_token)
    other, other_token = create_user(client, admin_token)
    a = _file(client, member_token, "5901234123457").json()
    b = _file(client, member_token, "4006381333931").json()
    _file(client, other_token, "5901234999991")

    r = client.get("/api/scan-requests/mine", headers=auth(member_token))
    assert r.status_code == 200, r.text
    ids = [req["id"] for req in r.json()]
    assert ids == [b["id"], a["id"]]  # newest first, only own


def test_staff_queue_filters_and_members_are_403(client, admin_token):
    member, member_token = create_user(client, admin_token)
    staff, staff_token = create_user(client, admin_token, role="staff")
    open_req = _file(client, member_token, "5901234123457").json()
    gone = _file(client, member_token, "4006381333931").json()
    client.post(f"/api/scan-requests/{gone['id']}/dismiss",
                headers=auth(admin_token))

    assert client.get("/api/scan-requests",
                      headers=auth(member_token)).status_code == 403

    r = client.get("/api/scan-requests", headers=auth(staff_token))
    assert r.status_code == 200, r.text
    assert [req["id"] for req in r.json()] == [open_req["id"]]  # default: open

    r = client.get("/api/scan-requests",
                   params={"status": "dismissed"},
                   headers=auth(staff_token))
    assert [req["id"] for req in r.json()] == [gone["id"]]

    r = client.get("/api/scan-requests", params={"status": "all"},
                   headers=auth(staff_token))
    assert len(r.json()) == 2

    r = client.get("/api/scan-requests", params={"status": "bogus"},
                   headers=auth(staff_token))
    assert r.status_code == 422


# --- resolve / dismiss ---------------------------------------------------------------


def test_resolve_links_the_item_and_records_audit(client, admin_token):
    member, member_token = create_user(client, admin_token)
    staff, staff_token = create_user(client, admin_token, role="staff")
    request = _file(client, member_token, "5901234123457",
                    name="Polish biscuit").json()
    item = _create_item(client, admin_token, "NEW-1", barcode="5901234123457")

    r = client.post(f"/api/scan-requests/{request['id']}/resolve",
                    json={"item_id": item["id"]}, headers=auth(staff_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "resolved"
    assert body["resolved_item_id"] == item["id"]
    assert body["resolved_by"] == staff["id"]
    assert body["resolved_at"] is not None
    # Default add_to_cart=false: resolution is administrative — the
    # requester's cart is untouched, and stock never moves.
    cart = client.get("/api/cart", headers=auth(member_token)).json()
    assert cart["lines"] == []


def test_resolve_with_add_to_cart_closes_the_loop(client, admin_token):
    member, member_token = create_user(client, admin_token)
    request = _file(client, member_token, "5901234123457").json()
    item = _create_item(client, admin_token, "NEW-2", barcode="5901234123457")

    r = client.post(
        f"/api/scan-requests/{request['id']}/resolve",
        json={"item_id": item["id"], "add_to_cart": True},
        headers=auth(admin_token),
    )
    assert r.status_code == 200, r.text
    cart = client.get("/api/cart", headers=auth(member_token)).json()
    assert [line["item"]["id"] for line in cart["lines"]] == [item["id"]]
    assert cart["lines"][0]["qty"] == 1
    # Stock untouched (coordinator decision 6).
    detail = client.get(f"/api/items/{item['id']}",
                        headers=auth(admin_token)).json()
    assert detail["stock"] == item["stock"]


def test_resolve_and_dismiss_error_cases(client, admin_token):
    member, member_token = create_user(client, admin_token)
    staff, staff_token = create_user(client, admin_token, role="staff")
    request = _file(client, member_token, "5901234123457").json()

    # Members cannot resolve or dismiss.
    assert client.post(f"/api/scan-requests/{request['id']}/resolve",
                       json={"item_id": 1},
                       headers=auth(member_token)).status_code == 403
    assert client.post(f"/api/scan-requests/{request['id']}/dismiss",
                       headers=auth(member_token)).status_code == 403

    # Unknown request / unknown item.
    assert client.post("/api/scan-requests/9999/resolve",
                       json={"item_id": 1},
                       headers=auth(staff_token)).status_code == 404
    item = _create_item(client, admin_token, "NEW-3")
    assert client.post(f"/api/scan-requests/{request['id']}/resolve",
                       json={"item_id": 999999},
                       headers=auth(staff_token)).status_code == 404

    # Dismiss, then every transition off a non-open state is a 409.
    assert client.post(f"/api/scan-requests/{request['id']}/dismiss",
                      headers=auth(staff_token)).status_code == 200
    for path in ("dismiss", "resolve"):
        r = client.post(f"/api/scan-requests/{request['id']}/{path}",
                        json={"item_id": item["id"]} if path == "resolve" else None,
                        headers=auth(staff_token))
        assert r.status_code == 409, (path, r.text)
        assert r.json()["detail"]["status"] == "not_open"


# --- the full loop -----------------------------------------------------------------


def test_scan_of_a_resolved_code_now_adds_to_cart(client, admin_token):
    member, member_token = create_user(client, admin_token)
    # Member scans an unknown code…
    r = client.post("/api/cart/scan", json={"code": "5901234123457"},
                    headers=auth(member_token))
    assert r.status_code == 404, r.text
    # …files the request…
    request = _file(client, member_token, "5901234123457").json()
    # …staff create the item and resolve…
    item = _create_item(client, admin_token, "LOOP-1", barcode="5901234123457")
    client.post(f"/api/scan-requests/{request['id']}/resolve",
                json={"item_id": item["id"]}, headers=auth(admin_token))
    # …and the member's next scan of the same code adds it.
    r = client.post("/api/cart/scan", json={"code": "5901234123457"},
                    headers=auth(member_token))
    assert r.status_code == 200, r.text
    assert r.json()["item"]["id"] == item["id"]


# --- cleanup & weak-ref tolerance ----------------------------------------------------


def test_member_deletion_cleans_requests(client, admin_token):
    member, member_token = create_user(client, admin_token)
    _file(client, member_token, "5901234123457")
    other, other_token = create_user(client, admin_token)
    other_req = _file(client, other_token, "4006381333931").json()

    r = client.delete(f"/api/members/{member['id']}",
                      headers=auth(admin_token))
    assert r.status_code == 204, r.text

    with Session(engine) as session:
        remaining = session.exec(select(ScanRequest)).all()
        assert [req.id for req in remaining] == [other_req["id"]]


def test_resolved_request_survives_item_deletion(client, admin_token):
    # resolved_item_id is a weak ref by design: the audit trail outlives the
    # catalogue entry, and the staff list must tolerate the dangling id.
    member, member_token = create_user(client, admin_token)
    request = _file(client, member_token, "5901234123457").json()
    item = _create_item(client, admin_token, "GONE-1", barcode="5901234123457")
    client.post(f"/api/scan-requests/{request['id']}/resolve",
                json={"item_id": item["id"]}, headers=auth(admin_token))

    assert client.delete(f"/api/items/{item['id']}",
                         headers=auth(admin_token)).status_code == 204

    r = client.get("/api/scan-requests", params={"status": "resolved"},
                   headers=auth(admin_token))
    assert r.status_code == 200, r.text
    entry = r.json()[0]
    assert entry["resolved_item_id"] == item["id"]  # the historical record
