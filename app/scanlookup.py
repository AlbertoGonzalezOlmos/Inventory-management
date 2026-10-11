"""The one scan-resolution decision table, shared by the API and the bridge.

Extracted from ``scripts/scanner_bridge.py::find_item`` (whose signature,
return contract and tests are preserved verbatim — that suite is the adapter
parity guarantee). A scanned code resolves to a catalogue item by EXACT
identity only:

- ``gtin`` / ``restricted`` → the canonical GTIN-14 in ``items.barcode``;
- ``opaque``               → exact, case-insensitive SKU equality;
- ``invalid-gtin`` / ``empty`` → NEVER looked up (a misread must not become
  a phantom identity or a wrong match);
- a keyword hit is a match ONLY under an explicit ``fuzzy=True`` (the
  bridge's ``--fuzzy`` back-office opt-in — the server never passes it).

There is deliberately no silent fallback to "the first search hit"
(REVIEW-m10.md P1: a partial word once resolved to the water bottle and
decremented its stock). Both callers inject an :class:`ItemLookup`: the
bridge an HTTP-backed adapter over its tested ``get`` seam, the server the
session-backed ``DbLookup`` in ``app/routers/items.py`` — which lives
server-side precisely so THIS module stays stdlib-pure: the bridge imports
it, and the bridge's documented contract is "Stdlib only, except the
optional pyserial". Do not add ORM/server imports here (pinned by an AST
test in tests/test_scanlookup.py). Because both callers literally call
:func:`resolve_scan`, the two paths cannot drift on identity. Any future
consumer (Task 5's flows, the Phase-B relay) must call this too, not
re-derive the table.

This module also owns the human-readable verdict messages (one wording for
the bridge's stdout and the API's 404 detail — parity of language, not just
of behaviour).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.barcodes import ScanAnalysis, analyze_scan


class ItemLookup(Protocol):
    """The three lookups the decision table needs.

    All three return *lists* (both backends naturally produce pages/lists);
    ``resolve_scan`` takes the first hit. The exactness doctrine lives in
    the contracts below and is pinned for both backends by the parity suite
    (tests/test_scanlookup.py):

    - ``by_barcode(gtin14)`` — items whose canonical GTIN-14 is exactly
      ``gtin14`` (an EAN/UPC in any representation must have been
      normalised by the caller — ``analyze_scan`` already does);
    - ``by_sku(sku)`` — items whose SKU equals ``sku`` case-insensitively
      (EXACT equality; a substring hit is not an identity);
    - ``search(q)`` — keyword hits in the API's ``?q=`` semantics. Only
      ever called with ``fuzzy=True``; the server never enables it.
    """

    def by_barcode(self, gtin14: str) -> list: ...
    def by_sku(self, sku: str) -> list: ...
    def search(self, q: str) -> list: ...


@dataclass(frozen=True)
class ScanVerdict:
    """The outcome of resolving one scanned code.

    ``item`` is whatever the injected lookup returned (an ORM ``Item`` for
    the server, a JSON dict for the bridge) — backend-opaque by design.
    """

    item: Any | None
    match: str | None       # "barcode" | "sku" | "search" | None
    analysis: ScanAnalysis


def resolve_scan(
    lookup: ItemLookup,
    code: str,
    *,
    symbology: str = "",
    fuzzy: bool = False,
) -> ScanVerdict:
    """Resolve one scanned code to a catalogue item — or to nothing.

    Identity or nothing, never a guess (REVIEW-m10.md P1). ``fuzzy`` is the
    bridge's explicit ``--fuzzy`` opt-in; the server NEVER passes it.
    """
    analysis = analyze_scan(code, symbology)

    if analysis.kind in ("gtin", "restricted"):
        # A GTIN is a global product identity: exact canonical match only.
        # (UPC-E payloads arrive pre-expanded by analyze_scan when the
        # scanner's symbology says so — the only honest way to know a bare
        # 6-digit payload is a zero-suppressed UPC-E.)
        hits = lookup.by_barcode(analysis.key)
        return ScanVerdict(hits[0] if hits else None,
                           "barcode" if hits else None, analysis)

    if analysis.kind in ("invalid-gtin", "empty"):
        # Misread / nothing: NEVER looked up — zero calls on the lookup, so
        # a scanning error cannot become a phantom identity or a wrong
        # match. Pinned by test at both the core and bridge levels.
        return ScanVerdict(None, None, analysis)

    # Opaque payload (Code 128 SKU label, internal code): exact SKU equality
    # only. A short all-digit payload is NOT guessed into a GTIN —
    # app.barcodes refuses to expand one without the scanner's word, and
    # this table must not undermine that by trying both number systems.
    hits = lookup.by_sku(analysis.raw)
    if hits:
        return ScanVerdict(hits[0], "sku", analysis)
    if fuzzy:
        hits = lookup.search(analysis.raw)
        if hits:
            return ScanVerdict(hits[0], "search", analysis)
    return ScanVerdict(None, None, analysis)


def verdict_message(analysis: ScanAnalysis) -> str:
    """The human-readable verdict for an unresolved scan.

    The bridge's stdout wording, verbatim (it used to build this inline in
    its on_scan) — the API's 404 detail now carries the same strings, so a
    kiosk operator and a bridge log reader see the same language.
    """
    message = {
        "invalid-gtin": "MISREAD — check digit fails; re-scan, do not file",
        "opaque": "no catalogue match (opaque payload)",
        "gtin": "valid GTIN, not in the catalogue — new-entry candidate",
        "restricted": "valid in-store GTIN, not in the catalogue — "
                      "new-entry candidate (restricted circulation)",
    }.get(analysis.kind, "no catalogue match")
    if analysis.kind == "opaque":
        raw = analysis.raw
        if raw.isdigit() and len(raw) in (6, 7, 8):
            message += (" — if this is a UPC-E, the scanner must transmit "
                        "its symbology ID: re-run with --symbology UPC-E")
    if analysis.kind in ("gtin", "restricted") and analysis.info is not None:
        where = analysis.info.region or "unknown GS1 region"
        message += f" ({where}, {analysis.info.usage})"
    return message
