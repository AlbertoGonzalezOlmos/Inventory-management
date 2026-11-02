"""Item 6: the error-code registry ↔ the wire ↔ the Danish copy.

One contract, three faces:

* **registry ↔ copy source** — every code in ``app/error_codes.py`` has an
  ``error.<code>`` row, the row's English cell IS the registry message (i.e.
  the string today's body carries), the Danish cell is a translation, and the
  row's placeholders are exactly the message's (so the browser's vars line up).
* **the wire** — a driven error carries ``X-Error-Code`` (and
  ``X-Error-Vars`` when the message is parameterised), a 200 carries neither,
  and the BODY is byte-identical to the registry's English. The existing
  per-endpoint ``detail == "..."`` assertions are the other half of that pin
  and are deliberately NOT touched by this file.
* **the render** — the browser can actually produce the Danish sentence: the
  code's Danish cell, interpolated with the vars from the header, is non-empty
  and different from the server's English. The full runtime path
  (``errorText`` -> row) is executed in ``tests/js/i18n.test.mjs``; here the
  pytest-meaningful claim is that the copy and the vars exist for every case a
  user can hit.

Driven through the real API (TestClient + the seeded database), never by
inspecting source: a code that no endpoint emits, or a header a route forgets,
must go red here.
"""

from __future__ import annotations

import importlib.util
import sys
from urllib.parse import parse_qsl

import pytest

from app import error_codes
from app.error_codes import ERRORS, HEADER_CODE, HEADER_VARS
from tests.conftest import auth, create_user

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


# --- the copy source ---------------------------------------------------------


def _load_i18n():
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(
        "_i18n_under_test", REPO_ROOT / "scripts" / "_i18n.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_i18n = _load_i18n()
ROWS, _PROBLEMS = _i18n.read_rows()
BY_ID = {row.id: row for row in ROWS}
TOKEN = __import__("re").compile(r"\{([a-z][a-z0-9_]*)\}")


def _vars_from_header(response) -> dict[str, str]:
    raw = response.headers.get(HEADER_VARS, "")
    return dict(parse_qsl(raw, keep_blank_values=True)) if raw else {}


def _render(cell: str, values: dict[str, str]) -> str:
    """Fill {tokens} the way static/i18n.js does (unknown tokens stay, so a
    missing var is visible to an assertion instead of rendering blank)."""
    def sub(match):
        name = match.group(1)
        return str(values[name]) if name in values else match.group(0)
    return TOKEN.sub(sub, cell)


def _da(response, extra: dict[str, str] | None = None) -> str:
    """The Danish copy the browser would render for this response.

    ``extra`` is for a var the browser derives from the BODY rather than the
    header — today only ``validation_field``'s field LABEL (from
    ``detail[0].loc``, mapped through static/i18n.js's FIELD_LABELS)."""
    code = response.headers[HEADER_CODE]
    row = BY_ID[f"error.{code}"]
    return _render(row.da, {**_vars_from_header(response), **(extra or {})})


# --- registry ↔ copy ---------------------------------------------------------


def test_every_registry_code_has_both_languages():
    """One `error.*` row per registry code: en = the message the body carries,
    da = a translation (never a copy of the English cell — that is how a
    forgotten translation looks), same placeholders."""
    for code, entry in ERRORS.items():
        rid = f"error.{code}"
        assert rid in BY_ID, f"{code}: no {rid} row in i18n/strings.csv"
        row = BY_ID[rid]
        assert row.en == entry.text, (
            f"{rid}: the English cell drifted from the registry message "
            f"(en={row.en!r} registry={entry.text!r})")
        assert row.da.strip(), f"{rid}: empty Danish cell"
        assert row.da != row.en, f"{rid}: the Danish cell was never translated"
        assert sorted(TOKEN.findall(row.en)) == sorted(TOKEN.findall(row.da)), (
            f"{rid}: placeholder mismatch between en and da")
        assert sorted(TOKEN.findall(row.en)) == sorted(TOKEN.findall(entry.message)), (
            f"{rid}: the row's placeholders disagree with the registry message")


def test_registry_entries_are_well_formed():
    """Statuses are real HTTP statuses, codes are wire tokens (never UI ids)."""
    for code, entry in ERRORS.items():
        assert 400 <= entry.status <= 599, f"{code}: {entry.status} is not an error status"
        assert code.islower() and "." not in code, (
            f"{code}: a wire code is lower_snake and never an i18n id")
        assert entry.message.strip() and entry.message == entry.message.strip()
    # The two 422 envelopes FastAPI itself produces, and the 500 handler's.
    assert {"validation", "validation_field", "internal_error"} <= set(ERRORS)


def test_the_client_only_rows_still_exist():
    """`error.request_failed` is the browser's last step (no server code) and
    the three copy rules that pin it are in the CSV too."""
    for rid in ("error.request_failed",):
        assert rid in BY_ID, rid
        assert BY_ID[rid].da.strip() and BY_ID[rid].da != BY_ID[rid].en


# --- driven cases ------------------------------------------------------------


def _assert_coded(response, code: str) -> None:
    """The header names the registry entry, and the browser's copy exists."""
    assert response.status_code == ERRORS[code].status, (
        f"{code}: expected {ERRORS[code].status}, got {response.status_code}")
    assert response.headers.get(HEADER_CODE) == code, (
        f"missing/wrong {HEADER_CODE}: {response.headers.get(HEADER_CODE)!r}")
    needed = sorted(TOKEN.findall(ERRORS[code].message))
    if needed and code != "validation_field":
        # A parameterised sentence must publish its values, or the browser
        # would render "…: {sku}" instead of the sentence. FastAPI's 422 is the
        # one exception: its `field` comes from the body's `detail[0].loc`
        # (`apiError` in static/app.js), not from a header — the body IS the
        # value's source there.
        assert sorted(_vars_from_header(response)) == needed, (
            f"{code}: expected {HEADER_VARS} with {needed}")
    da = _da(response)
    assert da.strip(), f"{code}: the Danish render is blank"
    assert "{" not in da, f"{code}: unfilled placeholder in {da!r}"
    en = _render(BY_ID[f"error.{code}"].en, _vars_from_header(response))
    assert da != en, f"{code}: the render did not switch language"


def _details(response):
    return response.json().get("detail")


def test_401_unauthenticated_is_coded_and_body_unchanged(client):
    r = client.get("/api/items")
    _assert_coded(r, "not_authenticated")
    assert _details(r) == "Not authenticated"          # the frozen body
    assert _da(r) == "Du er ikke logget ind"


def test_401_bad_token_is_coded(client):
    r = client.get("/api/items", headers=auth("not-a-real-token"))
    _assert_coded(r, "token_invalid")
    assert _details(r) == "Token invalid or expired"


def test_403_staff_only_is_coded(client, admin_token):
    _, member = create_user(client, admin_token)
    r = client.post("/api/items", json={"sku": "X", "name": "x"},
                    headers=auth(member))
    _assert_coded(r, "staff_required")
    assert _details(r) == "Staff privileges required"
    assert _da(r) == "Kræver personaleadgang"


def test_403_password_change_required_is_coded(client, admin_token):
    from tests.test_cart import _flagged_token
    flagged = _flagged_token(client, admin_token)
    r = client.get("/api/items", headers=auth(flagged))
    _assert_coded(r, "password_change_required")
    assert _details(r).startswith("Password change required: change your password")


def test_404_item_not_found_is_coded(client, admin_token):
    r = client.get("/api/items/999999", headers=auth(admin_token))
    _assert_coded(r, "item_not_found")
    assert _details(r) == "Item not found"
    assert _da(r) == "Varen blev ikke fundet"


def test_404_member_not_found_is_coded(client, admin_token):
    r = client.get("/api/members/999999/qr-badge", headers=auth(admin_token))
    _assert_coded(r, "member_not_found")
    assert _details(r) == "Member not found"


def test_409_email_taken_is_coded(client, admin_token):
    user, _ = create_user(client, admin_token)
    r = client.post("/api/members",
                    json={"name": "Dup", "email": user["email"],
                          "password": "password123", "role": "member"},
                    headers=auth(admin_token))
    _assert_coded(r, "email_taken")
    assert _details(r) == "Email already registered"
    assert _da(r) == "E-mailen er allerede registreret"


def test_409_parameterised_barcode_conflict_publishes_its_vars(client, admin_token):
    """The sentence names the other SKU: the header must carry it, and the
    Danish render must substitute it (never show "{sku}")."""
    ean = "5901234123457"
    first = client.post("/api/items",
                        json={"sku": "SKU-A", "name": "A", "barcode": ean},
                        headers=auth(admin_token))
    assert first.status_code == 201, first.text
    r = client.post("/api/items",
                    json={"sku": "SKU-B", "name": "B", "barcode": ean},
                    headers=auth(admin_token))
    _assert_coded(r, "barcode_taken")
    assert _details(r) == "Barcode already assigned to item SKU-A"
    assert _vars_from_header(r) == {"sku": "SKU-A"}
    assert _da(r) == "Stregkoden er allerede tildelt varen SKU-A"


def test_409_scan_request_duplicate_is_coded_body_untouched(client, admin_token):
    _, member = create_user(client, admin_token)
    body = {"code": "OPAQUE-1", "symbology": "", "name": "Thing",
            "price_cents": 0}
    first = client.post("/api/scan-requests", json=body, headers=auth(member))
    assert first.status_code == 201, first.text
    r = client.post("/api/scan-requests", json=body, headers=auth(member))
    _assert_coded(r, "intake_duplicate")
    detail = _details(r)
    assert detail["status"] == "duplicate"            # the SPA's discriminator
    assert detail["message"] == ERRORS["intake_duplicate"].message
    assert detail["request"]["code"] == "OPAQUE-1"


def test_422_validation_field_is_coded_with_fastapis_body(client, admin_token):
    """FastAPI's own 422: the body is FastAPI's (a list of {loc, msg, type}),
    the code names the field-bearing arm, and the field label resolves."""
    r = client.post("/api/items",
                    json={"sku": "NEG", "name": "Neg", "price_cents": -1},
                    headers=auth(admin_token))
    assert r.status_code == 422
    assert r.headers.get(HEADER_CODE) == "validation_field"
    detail = _details(r)
    assert isinstance(detail, list) and detail[0]["loc"] == ["body", "price_cents"]
    assert detail[0]["msg"] == "Input should be greater than or equal to 0"
    # The field travels in the BODY (loc[-1]): the runtime maps it to the
    # label row and fills the sentence, so the Danish render is complete.
    assert _da(r, {"field": BY_ID["common.price"].da}) == "Ugyldig værdi for Pris"


def test_422_offset_without_limit_is_coded(client, admin_token):
    r = client.get("/api/items/vector-search?q=red&offset=1",
                   headers=auth(admin_token))
    _assert_coded(r, "offset_requires_limit")
    assert _details(r) == "offset requires a limit (omit limit to rank every item)"


def test_422_structured_misread_keeps_its_envelope(client, admin_token):
    r = client.post("/api/cart/scan",
                    json={"code": "4006381333932", "symbology": "", "qty": 1},
                    headers=auth(admin_token))
    assert r.status_code == 422
    assert r.headers.get(HEADER_CODE) == "scan_misread"
    detail = _details(r)
    assert detail["status"] == "misread"
    assert detail["message"] == ERRORS["scan_misread"].message
    assert set(detail) == {"status", "message", "analysis"}


def test_415_json_required_is_coded(client, monkeypatch):
    """The one-shot pairing endpoint refuses non-JSON content-types.

    The loopback gate runs first, so the test client (host "testclient") opts
    into the LAN switch the way a deployment would."""
    monkeypatch.setenv("HCRM_SCANNER_PAIR_ANY", "1")
    r = client.post("/api/scanner/pair/auto", content=b"",
                    headers={"Content-Type": "text/plain"})
    _assert_coded(r, "json_required")
    assert _details(r)["status"] == "json_required"


def test_404_no_pairing_file_is_coded(client, tmp_path, monkeypatch):
    """The kiosk's one-click pair with no offer file: a structured 404 whose
    message is registry copy (the SPA shows it verbatim when uncoded)."""
    monkeypatch.setenv("HCRM_SCANNER_PAIR_ANY", "1")
    monkeypatch.setenv("HCRM_SCANNER_PAIRING_FILE",
                       str(tmp_path / "no-offer-here.json"))
    body = client.post("/api/scanner/pair/auto", json={})
    _assert_coded(body, "no_pairing_file")
    detail = _details(body)
    assert detail["status"] == "no_pairing_file"
    assert detail["message"] == ERRORS["no_pairing_file"].message


def test_a_success_carries_no_error_code(client, admin_token):
    ok = client.get("/api/items", headers=auth(admin_token))
    assert ok.status_code == 200
    assert HEADER_CODE not in ok.headers and HEADER_VARS not in ok.headers
    login = client.post("/api/auth/login",
                        json={"email": "admin@shop.local",
                              "password": "admin-password-1"})
    assert login.status_code == 200
    assert HEADER_CODE not in login.headers


def test_401_wrong_password_is_coded(client):
    r = client.post("/api/auth/login",
                    json={"email": "admin@shop.local", "password": "wrong-password"})
    _assert_coded(r, "invalid_credentials")
    assert _details(r) == "Invalid email or password"
    assert _da(r) == "Forkert e-mail eller adgangskode"


def test_the_retry_after_header_survives_the_code(client, monkeypatch):
    """`api_error` MERGES the site's headers: the 503's Retry-After must not be
    replaced by the code (the check-and-merge lives in code_headers)."""
    from app.error_codes import code_headers
    merged = code_headers("stock_db_locked", None, {"Retry-After": "1"})
    assert merged == {HEADER_CODE: "stock_db_locked", "Retry-After": "1"}


# --- the 422 field labels ----------------------------------------------------


def test_the_422_field_labels_exist_and_are_mapped():
    """The SPA's field map names these rows; a missing one would render the raw
    pydantic field name in a Danish sentence."""
    js = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    for field, rid in (("name", "field.name"), ("sku", "field.sku"),
                       ("price_cents", "common.price"), ("qty", "common.qty"),
                       ("barcode", "field.barcode"), ("delta", "field.delta"),
                       ("image_url", "field.image_url"),
                       ("new_password", "field.new_password"),
                       ("current_password", "auth.current_password"),
                       ("email", "auth.email"), ("phone", "account.phone")):
        assert rid in BY_ID, rid
        assert f'{field}: "{rid}"' in js, f"{field} is not mapped in i18n.js"
        assert BY_ID[rid].da.strip(), rid


# --- the registry is the only source ----------------------------------------


def test_no_router_raises_a_plain_string_outside_the_registry():
    """A plain-string detail must go through `api_error` (code + message from
    one table). This is the guard against a NEW uncoded string error: any
    `HTTPException(... "<text>")` left in a router is a contract break."""
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if "__pycache__" in str(path) or "vendor" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func).split(".")[-1]
            if name != "HTTPException":
                continue
            args = [*node.args, *(kw.value for kw in node.keywords if kw.arg == "detail")]
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    offenders.append(f"{path}:{node.lineno}: {arg.value!r}")
    assert offenders == [], (
        "plain-string HTTPException outside the registry (use api_error):\n  "
        + "\n  ".join(offenders))
