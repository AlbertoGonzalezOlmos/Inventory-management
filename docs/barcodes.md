# Barcode number systems for the catalogue (GTIN / GS1)

Why this document exists: the scanner (see `scanner-opn2001.md`) downloads
**(symbology, payload)** records, and the catalogue needs two decisions per
scan — *what kind of number is this?* and *is it an item we already have, or
a new entry?* Both answers come from understanding the numbering system
behind retail barcodes. This is the reference; the executable version lives
in `app/barcodes.py` (used by the API and by `scripts/scan_intake.py`), with
known-answer tests in `tests/test_barcodes.py`.

---

## 1. Symbology ≠ number system

Two independent layers get conflated all the time:

* The **symbology** is the physical encoding — the bar patterns: EAN-13,
  UPC-A, UPC-E, EAN-8, Code 128, ITF-14, QR… The scanner tells you which one
  it read (OPN-2001 symbology ids, `scanner-opn2001.md` §4.5).
* The **number system** is the meaning of the digits. EAN/UPC-family
  symbologies all carry **GTINs** from the **GS1 system** — a global numbering
  scheme where every trade item gets exactly one number. Code 128/39/QR
  payloads are **opaque**: they mean whatever the printer decided (in our
  shop: the internal SKU).

So the first classification step is: *does this payload live in the GS1
number space, or is it somebody's private identifier?*

## 2. The GTIN family

GS1's **Global Trade Item Number** is one number space with four lengths;
every representation zero-pads into the canonical **GTIN-14**:

| Structure | Encoded by | Typical use | Padding to GTIN-14 |
|---|---|---|---|
| GTIN-8  | EAN-8 barcode | tiny packaging (gum, pencils, cigarettes) | 6 leading zeros |
| GTIN-12 | UPC-A (and zero-suppressed UPC-E) | US/Canada retail | 2 leading zeros |
| GTIN-13 | EAN-13 (a.k.a. EAN/JAN) | retail everywhere else | 1 leading zero |
| GTIN-14 | ITF-14, GS1-128, GS1 DataBar | cases/pallets (logistics, not POS) | native |

Digit layout of the canonical GTIN-14 `T1…T14`:

* **T1** — *indicator digit* (GTIN-14 only): 1–8 = packaging level of the
  contained product (case, pallet, …; there is **no** worldwide consensus on
  which level is which number), 9 = variable measure/quantity item, 0 = only
  valid for the embedded GTIN-8/12/13 forms.
* **T2…T13** — **GS1 company prefix** (allocated by the national GS1
  organisation to the brand owner) + **item reference** (allocated by the
  brand owner, recommended sequentially). The split point varies: company
  prefixes are 6–12 digits long.
* **T14** — **check digit** (GS1 standard modulo-10, §3).

The key property for a catalogue: **the GTIN identifies a trade item
globally and uniquely** — "any product, any manufacturer, any country" —
and *each packaging/variant of a product gets its own GTIN*. That is what
makes the same/new decision in §7 mechanical.

## 3. Check digit (GS1 standard modulo-10)

Weights alternate **3,1** starting with weight 3 on the last data digit (so
the check digit itself sits at weight 1):

```
check = (10 − Σ(dᵢ · wᵢ) mod 10) mod 10        wᵢ = 3,1,3,1,… from the right
```

Worked example — EAN-13 `400638133393x` (Stabilo Point 88):
`4·1+0·3+0·1+6·3+3·1+8·3+1·1+3·3+3·1+3·3+9·1+3·3 = 89` → next multiple of ten
is 90 → `x = 1` → **4006381333931**.

Because the weights are coprime to 10, this detects **every single-digit
error** and ~90 % of adjacent transpositions (all except when the swapped
digits differ by exactly 5). For intake that means: a payload with a failing
check digit is a **misread or a typo** — never file it, re-scan instead.

## 4. Number systems inside the GTIN space ("prefixes")

The leading digits of the GTIN-13 view (`T2…T14` of the GTIN-14) are the
**GS1 prefix**. They say who issued the number and what it may be used for.
The full range table is data in `app/barcodes.py::_PREFIX_RANGES`
(transcribed from the published GS1 company-prefix list and pinned by
tests); the ranges that change *behaviour*:

| Prefix (GTIN-13 view) | Meaning | Catalogue consequence |
|---|---|---|
| `000–019`, `060–139` | US/Canada (UPC-A compatible) | global identity ✓ |
| `020–029` | **restricted circulation** (region-defined, UPC-A "number system 2") | NOT global — see below |
| `030–039` | US drugs (National Drug Code) | global identity ✓ |
| `040–049` | **restricted circulation** (company-defined, UPC-A "number system 4": loyalty cards, store coupons) | NOT global |
| `200–299` | **restricted circulation** (in-store codes — the classic supermarket weighing-label range, e.g. `2PPPPPPPPPPPWC` with price/weight embedded) | NOT global |
| `300–379` France, `400–440` Germany, `450–459`/`490–499` Japan (JAN), `500–509` UK, **`840–849` Spain & Andorra**, `800–839` Italy, … | national GS1 organisations | global identity ✓ (region of *registration*, not of manufacture) |
| `977` | serial publications (ISSN) | identity of the *title*; the +2 issue add-on varies per issue |
| `978–979` | **Bookland** (ISBN-13; `979-0` = Musicland/ISMN) | one *edition* of one title — a reprint with a new ISBN is a new item |
| `980` | refund receipts | not a product |
| `981–983`, `990–999` | GS1 coupons | not a product |

**Restricted-circulation numbers** (GS1 General Specifications: prefixes
`2`-range and the UPC-A `02x`/`04x` forms; for EAN-8, **RCN-8** codes whose
first digit is 0 or 2) are issued *by a store or region to itself*. They are
perfectly fine catalogue keys **if this shop printed them** (own-brand goods,
deli/weighing counter) — but two different shops can and do use the same
number for different products, so intake flags them (`kind="restricted"`,
`globally_unique=False`) instead of silently treating them as GS1 identities.

**UPC-A leading digit** ("number system", historical UPC view of the same
rules): 0/1/6–9 regular products, **2** variable-weight local use (the
embedded price/weight digits vary per package → the *item number* part is
the identity, not the whole code), **3** drugs (NDC), **4** in-store local
use, **5** coupons.

Which *company* owns a given prefix (useful when a new-item report should
say who the supplier is): **GEPIR**, https://gepir.gs1.org — free lookup of
GS1 company prefix → company name and country.

## 5. UPC-E: the same GTIN-12, zero-suppressed

Small packages get a 6-digit **UPC-E** instead of the 12-digit UPC-A. It is
*not a different number*: zeros in the manufacturer/product parts are
suppressed, and the number-system digit (0 or 1) plus the check digit are
encoded in the **parity pattern** of the bars (which the scanner decodes for
you). Expansion (d = the six digits, keyed on the last one):

| last digit | UPC-A equivalent (NS = number system 0/1) |
|---|---|
| 0 | `NS d1d2 000 00 d3d4d5 C` |
| 1 | `NS d1d2 100 00 d3d4d5 C` |
| 2 | `NS d1d2 200 00 d3d4d5 C` |
| 3 | `NS d1d2d3 00 000 d4d5 C` |
| 4 | `NS d1d2d3d4 0 0000 d5 C` |
| 5–9 | `NS d1d2d3d4d5 0000 d6 C` |

Example (GS1/Wikipedia): UPC-E `654321` with number system 0 → UPC-A
`065100004327`.

Intake consequence: **always expand before comparing** — the identical
product can arrive as `04252614` (UPC-E with preamble), `042100005264`
(UPC-A) or `0042100005264` (EAN-13 view). All three normalise to GTIN-14
`00042100005264`. The OPN-2001's *preamble* parameters (`0x24`/`0x25`,
`scanner-opn2001.md` §4.7) control whether it sends the 6-digit payload or
the 8-digit NS+payload+check form; `app/barcodes.py` accepts both (6-digit
expansion is only applied when the scanner *says* UPC-E — guessing from six
digits alone would turn every opaque 6-digit Code-128 payload into a fake
GTIN, because expansion computes its own check digit).

## 6. Add-on supplements (+2 / +5)

Magazines carry a 2-digit **issue number** and books a 5-digit **suggested
price** as a physically separate small barcode beside the main one; the
scanner reports them as combined symbologies (`EAN-13+2`, `UPC-A+5`, …).
Supplements are **not part of the product identity** (issue 5 and issue 6 of
the same magazine share the GTIN) — intake strips them and keeps the base
GTIN. The OPN-2001 delivers the parts either space-separated or
concatenated; both shapes are handled.

## 7. Same item, or new item? The decision rules

Everything above collapses into this table, which is exactly what
`scripts/scan_intake.py` implements (`app/barcodes.analyze_scan` +
catalogue lookup):

| # | Situation | Verdict |
|---|---|---|
| R1 | Payload normalises to a GTIN that **equals** an existing `items.barcode` (canonical GTIN-14) | **Same item.** One scan of one unit; N scans in a batch = counted quantity N. |
| R2 | Payload normalises to a GTIN **not** in the catalogue | **New item.** A GTIN is per *trade item*: a different size/colour/flavour, a reformulation, a new edition (ISBN!), a re-packaging — each legitimately gets its own GTIN. Never merge on "looks similar"; create the entry, pre-filled with the normalised barcode. |
| R3 | Digits, GTIN-shaped length (8/12/13/14), **check digit fails** | **Neither.** Misread or transcription error — re-scan; never create. |
| R4 | GTIN under a **restricted-circulation** prefix (`2xx`, `02x`, `04x`, RCN-8) | Same/new as R1/R2, but **only within our own numbering**: flag as store-issued; refuse to import such codes from external sources. |
| R5 | **Opaque** symbology (Code 128/39, QR, …): payload is a private identifier (our SKU labels, a supplier's part number) | Exact payload match against SKU/barcode = same item; otherwise new. The payload itself is the identity; there is no numbering authority behind it. |
| R6 | Two *different* GTINs sharing a **GS1 company prefix** | Different items (R2) that are **related products of the same brand** — useful for suggesting a category/supplier on the new entry; GEPIR resolves the prefix to the company name. |
| R7 | **GTIN-14 with indicator 1–8** (case/pallet code, ITF-14/GS1-128) | Not the retail unit: it is a packaging *of* an item whose GTIN-13 equals the code minus the indicator. Policy: link/annotate, don't silently file as a new retail product. |
| R8 | A digit payload from an **opaque** symbology that happens to validate as a GTIN | **Opaque wins** (R5). The scanner's symbology report is authoritative; a coincidental check-digit match is a 1-in-10 accident per code. |

Two corollaries worth internalising:

* **Duplicates inside one download are data, not errors** (stock counting:
  one scan per physical unit). Leave the OPN-2001's `reject_redundant`
  parameter off for counting sessions.
* **A barcode never tells you the product's name.** The GS1 prefix says
  *which country's registry* issued the company prefix — not where the
  product was made (a "84…" Spanish-prefix item can be manufactured
  anywhere). Name/description come from the human creating the entry; the
  GTIN only guarantees you are looking at the same trade item everyone else
  in the supply chain calls by that number.

## 8. How this is wired into HCRM

* `items.barcode` (unique, nullable) stores the **canonical GTIN-14** — one
  physical product cannot be filed twice under its EAN-13 and its UPC-E
  forms, because both normalise to the same string before the uniqueness
  check (migration for existing databases: `app/main.py::_migrate_schema`).
* API behaviour (`app/routers/items.py`, `app/schemas.py`):
  * `POST /api/items` / `PATCH /api/items/{id}` accept **any** GTIN form in
    `barcode` and store the canonical one; invalid check digit → **422**;
    barcode already on another item → **409** (with the owning SKU);
    `PATCH {"barcode": ""}` clears it. Internal SKU-style strings are
    rejected by the field — the `sku` column already covers those (R5).
  * `GET /api/items?barcode=…` is the exact-match lookup (input normalised
    first; invalid → 422, never a silent empty list).
  * The keyword search `q` also substring-matches `barcode`, so a
    keyboard-mode scanner typing digits into the SPA search box finds the
    item with zero extra integration.
* Tools:
  * `scripts/opn2001.py read --json` / `scripts/scanner_hid.py` — acquire
    scans (batch or live).
  * `scripts/scan_intake.py` — the decision engine of §7 against a read-only
    view of the catalogue; reports *same-item* (with counted quantity),
    *new-item* (with the number-system classification, suggested SKU, and a
    ready `POST /api/items` skeleton), and *invalid* (re-scan) groups.
    Creating entries stays an explicit staff action through the API/UI.

## 9. Sources (retrieved 2026-09-29)

* **GS1 General Specifications** — GTIN structure (indicator digit, company
  prefix, item reference, check digit), the modulo-10 algorithm, restricted
  circulation numbers incl. RCN-8 (§2.1.6.1), UPC-E zero-suppression rules.
* Wikipedia **"Global Trade Item Number"** — the four GTIN structures, the
  T1…T14 padding table, ISBN/Bookland rule, GEPIR pointer.
* Wikipedia **"International Article Number"** — check-digit worked example
  (Stabilo `4006381333931`), detection properties.
* Wikipedia **"Universal Product Code"** — number-system digit semantics
  (2/3/4/5), UPC-E expansion table and the `654321 → 065100004327 /
  165100004324` vectors, `042100005264 ≡ UPC-E 425261` figure.
* Wikipedia **"List of GS1 country codes"** — the prefix range list
  transcribed into `app/barcodes.py::_PREFIX_RANGES` (diffed against the
  article; do not extend the table from memory — 609 is Mauritius, Algeria
  is 613).
* Wikipedia **"EAN-8"** — GTIN-8 structure and RCN-8 (prefix 0/2).
* **GEPIR** (https://gepir.gs1.org) — live company-prefix registry lookups.

Every worked example above is pinned as a known-answer vector in
`tests/test_barcodes.py`.
