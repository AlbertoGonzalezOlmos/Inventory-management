"""The shared scan-resolution decision table (app/scanlookup.py).

Pins the doctrine extracted from the bridge's find_item — identity or
nothing, never a guess (REVIEW-m10.md P1) — at three levels:

- the pure decision table, with a recording fake lookup (which methods are
  called, with what, and how many times: a misread makes ZERO calls);
- the parity guarantee: one shared case list run against BOTH the DB-backed
  resolver and the bridge's HTTP adapter over the same catalogue — same
  input, same verdict, so the two paths cannot drift on identity;
- the bridge adapter's contract: it delegates to resolve_scan, and its
  memoisation keeps the pinned one-?q=-fetch behaviour on the opaque+fuzzy
  path.
"""

import app.scanlookup
import scripts.scanner_bridge as bridge
from app.database import engine
from app.routers.items import DbLookup
from app.scanlookup import resolve_scan
from scripts.scanner_bridge import find_item
from sqlmodel import Session
from tests.conftest import auth
from tests.test_scanner_bridge import fake_api


class RecordingLookup:
    """A fake ItemLookup that records every call — the zero-call pins for
    misreads/empty need to prove the table never even asked."""

    def __init__(self, by_barcode=None, by_sku=None, search=None):
        self.calls = []
        self._by_barcode = by_barcode or {}
        self._by_sku = by_sku or {}
        self._search = search or []

    def by_barcode(self, gtin14):
        self.calls.append(("by_barcode", gtin14))
        return self._by_barcode.get(gtin14, [])

    def by_sku(self, sku):
        self.calls.append(("by_sku", sku))
        # The ItemLookup contract: EXACT, case-insensitive SKU equality —
        # emulate it like the real resolvers do.
        return [item for key, items in self._by_sku.items()
                for item in items if key.lower() == sku.strip().lower()]

    def search(self, q):
        self.calls.append(("search", q))
        return self._search


ITEM = {"id": 7, "sku": "X", "name": "whatever"}


# --- the pure decision table -------------------------------------------------


def test_gtin_uses_the_barcode_lookup_only():
    lookup = RecordingLookup(by_barcode={"08412345678905": [ITEM]})
    verdict = resolve_scan(lookup, "8412345678905")
    assert verdict.item == ITEM and verdict.match == "barcode"
    assert verdict.analysis.kind == "gtin"
    assert verdict.analysis.key == "08412345678905"
    assert lookup.calls == [("by_barcode", "08412345678905")]


def test_unknown_gtin_reports_nothing():
    lookup = RecordingLookup()
    verdict = resolve_scan(lookup, "4006381333931")
    assert verdict.item is None and verdict.match is None
    assert lookup.calls == [("by_barcode", "04006381333931")]


def test_misread_makes_zero_lookup_calls():
    """A bad check digit must not even be looked up (phantom identity)."""
    lookup = RecordingLookup()
    verdict = resolve_scan(lookup, "8412345678906")
    assert verdict.item is None and verdict.match is None
    assert verdict.analysis.kind == "invalid-gtin"
    assert lookup.calls == []


def test_empty_makes_zero_lookup_calls():
    lookup = RecordingLookup()
    verdict = resolve_scan(lookup, "")
    assert verdict.analysis.kind == "empty"
    assert lookup.calls == []


def test_opaque_is_exact_sku_only():
    lookup = RecordingLookup(by_sku={"ex-007": [ITEM]})
    verdict = resolve_scan(lookup, "EX-007")  # case-insensitive at the resolver
    assert verdict.item == ITEM and verdict.match == "sku"
    assert verdict.analysis.kind == "opaque"


def test_keyword_is_never_called_without_fuzzy():
    """The server's contract: no guessing — a miss is a miss."""
    lookup = RecordingLookup()
    verdict = resolve_scan(lookup, "water")
    assert verdict.item is None and verdict.match is None
    assert lookup.calls == [("by_sku", "water")]  # no ("search", ...) call


def test_fuzzy_opt_in_takes_the_first_keyword_hit():
    lookup = RecordingLookup(search=[ITEM])
    verdict = resolve_scan(lookup, "water", fuzzy=True)
    assert verdict.item == ITEM and verdict.match == "search"
    assert lookup.calls == [("by_sku", "water"), ("search", "water")]


def test_upce_expansion_needs_the_scanners_word():
    lookup = RecordingLookup(by_barcode={"00042100005264": [ITEM]})
    verdict = resolve_scan(lookup, "425261", symbology="UPC-E")
    assert verdict.match == "barcode"
    assert lookup.calls == [("by_barcode", "00042100005264")]

    # Without the symbology the same payload is opaque: never guessed.
    lookup = RecordingLookup()
    verdict = resolve_scan(lookup, "425261")
    assert verdict.match is None and verdict.analysis.kind == "opaque"
    assert lookup.calls == [("by_sku", "425261")]


# --- parity: the DB resolver and the bridge adapter agree --------------------

PARITY_CASES = [
    # (code, symbology, fuzzy)
    ("8412345678905", "", False),    # known GTIN
    ("425261", "UPC-E", False),      # UPC-E → canonical identity
    ("425261", "", False),           # bare 6 digits: opaque, never guessed
    ("ex-007", "", False),           # seed catalogue item by SKU (case variant)
    ("mylabel-42", "", False),       # controlled opaque label
    ("fuzzytarget", "", False),      # no match anywhere
    ("fuzzytarget", "", True),       # fuzzy → first keyword hit
    ("4006381333931", "", False),    # valid, absent GTIN
    ("8412345678906", "", False),    # misread
    ("2012345678903", "", False),    # restricted-circulation match
]


def _create_item(client, token, **fields):
    r = client.post("/api/items", json=fields, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def test_parity_db_and_bridge_agree(client, admin_token):
    """Same catalogue, same cases, two backends (ORM session vs the bridge's
    HTTP adapter over the API's own JSON): same item, same match, same
    analysis. This is the no-drift guarantee made executable."""
    _create_item(client, admin_token, sku="BTL-001", name="Fuzzytarget Bottle",
                 price_cents=100, barcode="8412345678905")
    _create_item(client, admin_token, sku="TIN-001", name="Canned product",
                 price_cents=100, barcode="042100005264")
    _create_item(client, admin_token, sku="MYLABEL-42", name="Store-label product",
                 price_cents=100)
    _create_item(client, admin_token, sku="IN-001", name="In-store weighed goods",
                 price_cents=100, barcode="2012345678903")

    # The bridge's HTTP view of the SAME catalogue (the API's own ordering,
    # id DESC, is what ?q= returns and what the adapter's first-hit sees).
    table = client.get("/api/items?limit=100", headers=auth(admin_token)).json()
    get = fake_api(table)

    with Session(engine) as session:
        for code, symbology, fuzzy in PARITY_CASES:
            verdict = resolve_scan(DbLookup(session), code,
                                   symbology=symbology, fuzzy=fuzzy)
            item, how, analysis = find_item(get, code, fuzzy=fuzzy,
                                            symbology=symbology)
            assert (verdict.item.id if verdict.item else None) == \
                (item["id"] if item else None), (code, symbology, fuzzy)
            assert verdict.match == how, (code, symbology, fuzzy)
            assert verdict.analysis.kind == analysis.kind, (code, symbology, fuzzy)
            assert verdict.analysis.key == analysis.key, (code, symbology, fuzzy)


def test_bridge_find_item_delegates_to_resolve_scan(monkeypatch):
    """The adapter cannot be silently re-inlined: it must go through the
    shared table."""
    seen = []
    real = app.scanlookup.resolve_scan

    def spy(lookup, code, *, symbology="", fuzzy=False):
        seen.append((code, symbology, fuzzy))
        return real(lookup, code, symbology=symbology, fuzzy=fuzzy)

    monkeypatch.setattr(bridge, "resolve_scan", spy)
    find_item(fake_api([]), "8412345678905")
    assert seen == [("8412345678905", "", False)]


def test_bridge_opaque_fuzzy_fetches_the_q_page_once():
    """Memoisation pin: the SKU check and the fuzzy first-hit share ONE ?q=
    fetch (the pre-extraction bridge's behaviour; two fetches would be a
    silent behavioural change)."""
    table = [{"id": 1, "sku": "BTL-001", "name": "Fuzzytarget Bottle",
              "barcode": None}]
    get = fake_api(table)
    item, how, _ = find_item(get, "fuzzytarget", fuzzy=True)
    assert how == "search" and item["id"] == 1
    assert get.calls == ["/api/items?q=fuzzytarget&limit=100"]


def test_scanlookup_core_stays_stdlib_pure():
    """The bridge imports app.scanlookup, and its documented contract is
    "Stdlib only, except the optional pyserial" — so the core module must
    never grow ORM/server imports (the DbLookup-placement regression this
    pin exists for: the session backend lives in app/routers/items.py).
    AST-based, because inside the suite every heavy module is already in
    sys.modules and an import probe would prove nothing."""
    import ast
    import pathlib

    allowed = {"__future__", "dataclasses", "typing", "app.barcodes"}
    tree = ast.parse(pathlib.Path(app.scanlookup.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    assert imported <= allowed, (
        f"app/scanlookup.py must stay importable from the stdlib-only "
        f"bridge; offending imports: {sorted(imported - allowed)}")
