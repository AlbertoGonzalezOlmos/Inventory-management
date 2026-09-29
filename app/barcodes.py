"""Barcode number systems: GTIN validation, normalisation and classification.

The shop's scanner (Opticon OPN-2001, docs/scanner-opn2001.md) downloads
records of (symbology, payload). To turn a payload into catalogue knowledge we
must answer two questions:

1. **What number system does it use?** Retail product codes are GTINs from the
   GS1 system (EAN-13/EAN-8/UPC-A/UPC-E/ITF-14 all encode one). The GS1 prefix
   tells us whether the code identifies a product *globally* (a real GTIN),
   only *within a region or company* (restricted-circulation "in-store" codes,
   prefixes 2xx/02x/04x), or is something else entirely (ISBN, coupon, …).
   Internal labels (Code 128 with our SKU) carry no GS1 structure at all.

2. **Same item, or a new item?** A GTIN is a *global product identity*:
   after normalisation (UPC-E → UPC-A → zero-padded GTIN-14, add-ons
   stripped), an exact match means the same product, anywhere in the world.
   Any different GTIN is a different trade item — even for visually identical
   products (size/colour/flavour variants each get their own GTIN) — and
   therefore needs its own catalogue entry.

This module is pure stdlib and dependency-free so it can be used from the API
(app.routers.items), the intake tool (scripts/scan_intake.py) and tests alike.
Numbering facts: GS1 General Specifications §2.1 (GTIN structure, check digit,
restricted-circulation prefixes) and the GS1 company-prefix range list; see
docs/barcodes.md for the full derivation and references.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Valid GTIN payload lengths (before zero-padding to the canonical 14).
GTIN_LENGTHS = (8, 12, 13, 14)

# Symbology names (as reported by the OPN-2001, docs §4.5) whose payload is a
# GTIN. "Bookland" is EAN-13 with a 978/979 prefix (ISBN). Add-on variants
# (+2/+5) carry the base GTIN followed by the supplement.
GTIN_SYMBOLOGIES = {
    "UPC-A",
    "UPC-E",
    "UPC-E1",
    "EAN-8",
    "EAN-13",
    "Bookland",
    "ITF",          # ITF-14: logistics cases, GTIN-14
    "EAN-128",      # GS1-128: AI (01) carries a GTIN-14
    "RSS-14",       # GS1 DataBar Omnidirectional: GTIN-14
    "RSS Limited",
    "RSS Expanded",
    "Composite",
    "Coupon",       # UPC-A based
    "UPC-A+2", "UPC-E+2", "EAN-8+2", "EAN-13+2", "UPC-E1+2",
    "UPC-A+5", "UPC-E+5", "EAN-8+5", "EAN-13+5", "UPC-E1+5",
}

# Symbologies whose payload is an opaque identifier (SKUs, internal labels):
# a numeric-looking payload from these is NEVER interpreted as a GTIN, even
# if it would validate — the printer chose the digits, not GS1.
OPAQUE_SYMBOLOGIES = {
    "Code 39", "Code 39 Full ASCII", "Codabar", "Code 128", "D25", "IATA",
    "Code 93", "Code 11", "MSI", "Trioptic Code 39", "ISBT-128",
    "ISBT-128 concatenated", "Code 32", "PDF-417", "Macro PDF", "Data Matrix",
    "QR code", "Signature", "Postnet (US)", "Postbar (Canada)", "Postal (UK)",
    "Postal (Japan)", "Postal (Australia)",
}

# GS1 prefix ranges (first three digits of the GTIN-13 view). Transcribed from
# the published GS1 company-prefix range list (Wikipedia "List of GS1 country
# codes", mirroring the gs1.org registry; retrieved 2026-09-29 and diffed
# against it — do not extend from memory). Keep ascending: _prefix_lookup
# returns the FIRST match. Which company owns a prefix: GEPIR (gepir.gs1.org).
# (start, end_inclusive, region, usage)
_PREFIX_RANGES: list[tuple[int, int, str, str]] = [
    # UPC-A-compatible 2-digit "number systems" (GTIN-13 view starts with 0):
    (0, 19, "United States / Canada (UPC-A compatible)", "retail"),
    (20, 29, "restricted circulation (region-defined)", "restricted-region"),
    (30, 39, "United States (drugs, National Drug Code)", "drug-us"),
    (40, 49, "restricted circulation (company-defined)", "restricted-company"),
    (50, 59, "GS1 US reserved (UPC-A number system 5: coupons)", "reserved"),
    (60, 139, "United States / Canada", "retail"),
    # EAN-13 2-digit restricted circulation (in-store codes, weighing labels):
    (200, 299, "restricted circulation (in-store / region-defined)", "restricted-region"),
    # Country / territory ranges:
    (300, 379, "France & Monaco", "retail"),
    (380, 380, "Bulgaria", "retail"),
    (381, 381, "Kosovo", "retail"),
    (383, 383, "Slovenia", "retail"),
    (385, 385, "Croatia", "retail"),
    (387, 387, "Bosnia & Herzegovina", "retail"),
    (389, 389, "Montenegro", "retail"),
    (400, 440, "Germany", "retail"),
    (450, 459, "Japan (new JAN range)", "retail"),
    (460, 469, "Russia", "retail"),
    (470, 470, "Kyrgyzstan", "retail"),
    (471, 471, "Taiwan", "retail"),
    (474, 474, "Estonia", "retail"),
    (475, 475, "Latvia", "retail"),
    (476, 476, "Azerbaijan", "retail"),
    (477, 477, "Lithuania", "retail"),
    (478, 478, "Uzbekistan", "retail"),
    (479, 479, "Sri Lanka", "retail"),
    (480, 480, "Philippines", "retail"),
    (481, 481, "Belarus", "retail"),
    (482, 482, "Ukraine", "retail"),
    (483, 483, "Turkmenistan", "retail"),
    (484, 484, "Moldova", "retail"),
    (485, 485, "Armenia", "retail"),
    (486, 486, "Georgia", "retail"),
    (487, 487, "Kazakhstan", "retail"),
    (488, 488, "Tajikistan", "retail"),
    (489, 489, "Hong Kong", "retail"),
    (490, 499, "Japan (original JAN range)", "retail"),
    (500, 509, "United Kingdom", "retail"),
    (520, 521, "Greece", "retail"),
    (528, 528, "Lebanon", "retail"),
    (529, 529, "Cyprus", "retail"),
    (530, 530, "Albania", "retail"),
    (531, 531, "North Macedonia", "retail"),
    (535, 535, "Malta", "retail"),
    (539, 539, "Ireland", "retail"),
    (540, 549, "Belgium & Luxembourg", "retail"),
    (560, 560, "Portugal", "retail"),
    (569, 569, "Iceland", "retail"),
    (570, 579, "Denmark / Faroe Islands / Greenland", "retail"),
    (590, 590, "Poland", "retail"),
    (594, 594, "Romania", "retail"),
    (599, 599, "Hungary", "retail"),
    (600, 601, "South Africa", "retail"),
    (603, 603, "Ghana", "retail"),
    (604, 604, "Senegal", "retail"),
    (605, 605, "Uganda", "retail"),
    (606, 606, "Angola", "retail"),
    (607, 607, "Oman", "retail"),
    (608, 608, "Bahrain", "retail"),
    (609, 609, "Mauritius", "retail"),
    (611, 611, "Morocco", "retail"),
    (613, 613, "Algeria", "retail"),
    (615, 615, "Nigeria", "retail"),
    (616, 616, "Kenya", "retail"),
    (618, 618, "Ivory Coast", "retail"),
    (619, 619, "Tunisia", "retail"),
    (620, 620, "Tanzania", "retail"),
    (621, 621, "Syria", "retail"),
    (622, 622, "Egypt", "retail"),
    (624, 624, "Libya", "retail"),
    (625, 625, "Jordan", "retail"),
    (626, 626, "Iran", "retail"),
    (627, 627, "Kuwait", "retail"),
    (628, 628, "Saudi Arabia", "retail"),
    (629, 629, "United Arab Emirates", "retail"),
    (630, 630, "Qatar", "retail"),
    (631, 631, "Namibia", "retail"),
    (640, 649, "Finland", "retail"),
    (680, 681, "China", "retail"),
    (690, 699, "China", "retail"),
    (700, 709, "Norway", "retail"),
    (729, 729, "Israel", "retail"),
    (730, 739, "Sweden", "retail"),
    (740, 740, "Guatemala", "retail"),
    (741, 741, "El Salvador", "retail"),
    (742, 742, "Honduras", "retail"),
    (743, 743, "Nicaragua", "retail"),
    (744, 744, "Costa Rica", "retail"),
    (745, 745, "Panama", "retail"),
    (746, 746, "Dominican Republic", "retail"),
    (750, 750, "Mexico", "retail"),
    (754, 755, "Canada", "retail"),
    (759, 759, "Venezuela", "retail"),
    (760, 769, "Switzerland & Liechtenstein", "retail"),
    (770, 771, "Colombia", "retail"),
    (773, 773, "Uruguay", "retail"),
    (775, 775, "Peru", "retail"),
    (777, 777, "Bolivia", "retail"),
    (778, 779, "Argentina", "retail"),
    (780, 780, "Chile", "retail"),
    (784, 784, "Paraguay", "retail"),
    (786, 786, "Ecuador", "retail"),
    (789, 790, "Brazil", "retail"),
    (800, 839, "Italy / San Marino / Vatican City", "retail"),
    (840, 849, "Spain & Andorra", "retail"),
    (850, 850, "Cuba", "retail"),
    (858, 858, "Slovakia", "retail"),
    (859, 859, "Czechia", "retail"),
    (860, 860, "Serbia", "retail"),
    (865, 865, "Mongolia", "retail"),
    (867, 867, "North Korea", "retail"),
    (868, 869, "Turkey", "retail"),
    (870, 879, "Netherlands", "retail"),
    (880, 881, "South Korea", "retail"),
    (884, 884, "Cambodia", "retail"),
    (885, 885, "Thailand", "retail"),
    (888, 888, "Singapore", "retail"),
    (890, 890, "India", "retail"),
    (893, 893, "Vietnam", "retail"),
    (894, 894, "Bangladesh", "retail"),
    (896, 896, "Pakistan", "retail"),
    (899, 899, "Indonesia", "retail"),
    (900, 919, "Austria", "retail"),
    (930, 939, "Australia", "retail"),
    (940, 949, "New Zealand", "retail"),
    (950, 952, "GS1 Global Office special applications "
              "(950: territories without an MO, 951: EPC GID, 952: demos)",
     "special"),
    (955, 955, "Malaysia", "retail"),
    (958, 958, "Macau", "retail"),
    # Special numbering systems:
    (977, 977, "serial publications (ISSN)", "serial"),
    (978, 979, "Bookland (ISBN-13; 979-0 = Musicland/ISMN)", "book"),
    (980, 980, "refund receipts", "refund-receipt"),
    (981, 983, "GS1 coupons (common currency areas)", "coupon"),
    (990, 999, "GS1 coupons", "coupon"),
]

# Usages that do NOT identify a product globally (the code is only meaningful
# inside the region/company/store that issued it — e.g. a supermarket's own
# weighing labels under EAN-13 prefix 2xx).
NON_GLOBAL_USAGES = {
    "restricted-region",
    "restricted-company",
    "coupon",
    "refund-receipt",
    "reserved",
}


# --------------------------------------------------------------------------
# Check digit (GS1 standard modulo-10)
# --------------------------------------------------------------------------

def check_digit(digits: str) -> str:
    """GS1 modulo-10 check digit for a string of data digits (no check yet).

    Weights alternate 3,1 from the right: the last data digit gets weight 3,
    the check digit itself would get weight 1. Equivalent formulation used by
    the standard: check = (10 - Σ(dᵢ·wᵢ) mod 10) mod 10.
    """
    if not digits.isdigit():
        raise ValueError("check_digit expects digits only")
    total = 0
    for i, ch in enumerate(reversed(digits)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return str((10 - total % 10) % 10)


def is_valid_gtin(code: str) -> bool:
    """True if `code` is a structurally valid GTIN (right length + check)."""
    code = code.strip()
    if not code.isdigit() or len(code) not in GTIN_LENGTHS:
        return False
    return check_digit(code[:-1]) == code[-1]


# --------------------------------------------------------------------------
# UPC-E (zero-suppressed) expansion
# --------------------------------------------------------------------------

def upce_to_upca(code: str, number_system: str = "0") -> str:
    """Expand a UPC-E code to its 12-digit UPC-A equivalent.

    Accepts either the 6-digit suppressed payload ("425261") or the 8-digit
    transmission form with preamble (number system + 6 digits + check digit,
    "04252614" — the OPN-2001 sends this when the UPC-E *preamble* parameter
    is on). The number system digit of a UPC-E is 0 or 1 and is physically
    encoded in the parity pattern of the bars; a payload-only scan therefore
    defaults to number system 0 (the GS1 general case). The check digit is
    recomputed for payload input and verified for preamble input.

    Expansion table (GS1 General Specifications; d = the 6 payload digits,
    keyed on the last payload digit d6):
        d6=0:  NS d1d2 000  00 d3d4d5 C
        d6=1:  NS d1d2 100  00 d3d4d5 C
        d6=2:  NS d1d2 200  00 d3d4d5 C
        d6=3:  NS d1d2d3 00 000 d4d5 C
        d6=4:  NS d1d2d3d4 0 0000 d5 C
        d6=5..9: NS d1d2d3d4d5 0000 d6 C
    """
    code = code.strip()
    if not code.isdigit():
        raise ValueError("UPC-E must be digits")
    if len(code) == 8:  # NS + payload + check digit
        number_system, payload, check = code[0], code[1:7], code[7]
    elif len(code) == 6:
        payload = code
        check = None
    else:
        raise ValueError(f"UPC-E is 6 or 8 digits, got {len(code)}")
    if number_system not in "01":
        raise ValueError("UPC-E number system must be 0 or 1")

    d1, d2, d3, d4, d5, d6 = payload
    if d6 in "012":
        body = number_system + d1 + d2 + d6 + "00" + "00" + d3 + d4 + d5
    elif d6 == "3":
        body = number_system + d1 + d2 + d3 + "00" + "000" + d4 + d5
    elif d6 == "4":
        body = number_system + d1 + d2 + d3 + d4 + "0" + "0000" + d5
    else:  # 5..9 — d6 IS the product digit: NS + d1..d5 + "0000" + d6
        body = number_system + d1 + d2 + d3 + d4 + d5 + "0000" + d6
    computed = check_digit(body)
    if check is not None and check != computed:
        raise ValueError(
            f"UPC-E check digit mismatch: got {check}, computed {computed}"
        )
    return body + computed


# --------------------------------------------------------------------------
# Add-on supplements (+2 / +5)
# --------------------------------------------------------------------------

def split_addon(code: str, symbology: str = "") -> tuple[str, str]:
    """Split a scanned payload into (base code, add-on supplement).

    Magazine/price supplements are appended to the base EAN/UPC and are NOT
    part of the product identity — they must be stripped before matching.
    The OPN-2001 reports them either as distinct symbology ids (…+2/…+5,
    docs §4.5) with the parts joined by a space or concatenated, so both
    shapes are handled. Returns (code, "") when there is no supplement.

    Space-splitting is applied ONLY to the EAN/UPC family: an opaque payload
    (e.g. Code 128 "BOX 12") may legitimately contain spaces.
    """
    code = code.strip()
    base_symb = symbology.split("+")[0]
    gtin_family = base_symb in GTIN_SYMBOLOGIES
    if gtin_family and " " in code:
        base, _, addon = code.rpartition(" ")
        return base.strip(), addon.strip()
    if symbology.endswith("+2") and code.isdigit() and len(code) > 2:
        return code[:-2], code[-2:]
    if symbology.endswith("+5") and code.isdigit() and len(code) > 5:
        return code[:-5], code[-5:]
    return code, ""


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def normalize_gtin(code: str, symbology: str = "") -> str | None:
    """Normalise any EAN/UPC-family code to its canonical 14-digit GTIN.

    GTIN-8/12/13 are zero-padded on the left (GS1 General Specifications:
    all GTIN structures share the 14-digit number space); UPC-E is first
    expanded to UPC-A; add-on supplements are stripped. Returns None when the
    payload is not a valid GTIN (wrong length or bad check digit) — the caller
    decides what that means (misread vs internal SKU label).
    """
    base, _addon = split_addon(code, symbology)
    if not base:
        return None
    symb = symbology.split("+")[0]  # "UPC-E+2" → "UPC-E"

    # UPC-E expansion ONLY on the scanner's word: a bare 6-digit payload from
    # any symbology would otherwise always "pass" (expansion computes its own
    # check digit), turning opaque numeric codes into fake GTINs.
    if symb in ("UPC-E", "UPC-E1"):
        try:
            base = upce_to_upca(base, number_system="1" if symb == "UPC-E1" else "0")
        except ValueError:
            return None

    # GS1-128 payloads may carry the "(01)" application identifier in text.
    if base.startswith("(01)"):
        base = base[4:]

    if not base.isdigit() or len(base) not in GTIN_LENGTHS:
        return None
    if check_digit(base[:-1]) != base[-1]:
        return None
    return base.rjust(14, "0")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BarcodeInfo:
    """What a normalised GTIN tells us about the product identity."""

    gtin14: str                      # canonical 14-digit GTIN
    format: str                      # "GTIN-8" | "GTIN-12" | "GTIN-13" | "GTIN-14"
    indicator: str | None = None     # GTIN-14 packaging-level digit (else None)
    prefix: str | None = None        # GS1 prefix used for the region lookup
    region: str | None = None        # "Spain & Andorra", "restricted …", …
    usage: str = "retail"            # see _PREFIX_RANGES / NON_GLOBAL_USAGES
    globally_unique: bool = True     # False for restricted-circulation codes
    notes: list[str] = field(default_factory=list)

    @property
    def in_store(self) -> bool:
        """Code issued by a store/region/company, not by GS1 for a product."""
        return not self.globally_unique


def _prefix_lookup(prefix3: int) -> tuple[str, str] | None:
    for start, end, region, usage in _PREFIX_RANGES:
        if start <= prefix3 <= end:
            return region, usage
    return None


def classify_gtin(gtin14: str, orig_len: int | None = None) -> BarcodeInfo:
    """Classify a canonical 14-digit GTIN (from normalize_gtin).

    `orig_len` is the length of the scanned payload before padding (8/12/13/
    14). It disambiguates the encoded format when padding alone cannot — e.g.
    a UPC-A with six leading zeros would otherwise look like a GTIN-8. When
    omitted (classifying a stored barcode), the format is inferred.
    """
    if not (isinstance(gtin14, str) and gtin14.isdigit() and len(gtin14) == 14):
        raise ValueError("classify_gtin expects a 14-digit GTIN")

    if orig_len in (8, 12, 13, 14):
        fmt = {8: "GTIN-8", 12: "GTIN-12", 13: "GTIN-13", 14: "GTIN-14"}[orig_len]
    elif gtin14[:6] == "000000" and is_valid_gtin(gtin14[6:]):
        fmt = "GTIN-8"
    else:
        significant = len(gtin14.lstrip("0")) or 1
        fmt = "GTIN-14" if significant > 13 else (
            "GTIN-12" if significant <= 12 else "GTIN-13"
        )

    if fmt == "GTIN-8":
        view = gtin14[6:]          # the 8-digit view carries the GS1 prefix
    else:
        view = gtin14[1:]          # GTIN-13 view (T2..T14 of the GTIN-14)
    prefix3 = int(view[:3])

    notes: list[str] = []
    indicator = None
    if fmt == "GTIN-14":
        indicator = gtin14[0]
        if indicator == "9":
            notes.append("indicator 9: variable measure/quantity trade item")
        elif indicator != "0":
            notes.append(f"indicator {indicator}: packaging level (case/pallet), "
                         "not the retail unit")

    region, usage = (None, "retail")
    if fmt == "GTIN-8" and view[0] in "02":
        # RCN-8: restricted circulation number (store's own-brand items),
        # GS1 General Specifications §2.1.6.1 — prefix 0 or 2.
        region = "restricted circulation (RCN-8: store own-brand)"
        usage = "restricted-company"
    else:
        found = _prefix_lookup(prefix3)
        if found:
            region, usage = found
        else:
            region = f"GS1 prefix {prefix3:03d} (unassigned / see GEPIR)"

    if usage == "book":
        notes.append("Bookland ISBN-13 (978/979): identifies one edition of "
                     "one title — a different edition is a different item")
    if usage == "serial":
        notes.append("ISSN serial: the +2 add-on (issue number) varies per "
                     "issue but the base code identifies the title")
    if usage == "drug-us":
        notes.append("US drug: middle 10 digits are the National Drug Code")

    globally_unique = usage not in NON_GLOBAL_USAGES
    if not globally_unique:
        notes.append("restricted-circulation code: only meaningful inside the "
                     "region/company/store that issued it — safe as an item "
                     "identity ONLY if this shop issued it")
    if usage == "retail" and fmt == "GTIN-13" and (
            450 <= prefix3 <= 459 or 490 <= prefix3 <= 499):
        notes.append("Japanese Article Number (JAN)")

    prefix = f"{prefix3:03d}"
    return BarcodeInfo(
        gtin14=gtin14, format=fmt, indicator=indicator, prefix=prefix,
        region=region, usage=usage, globally_unique=globally_unique,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Whole-scan analysis (the entry point used by scan intake)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScanAnalysis:
    """Everything the catalogue needs to know about one scanned payload.

    kind:
      * "gtin"         — valid global product identity (see .info)
      * "restricted"   — valid GTIN but restricted circulation (in-store)
      * "invalid-gtin" — looks like a GTIN (digits, plausible length) but the
                         check digit fails: misread or typo. NEVER create a
                         catalogue entry from it; re-scan instead.
      * "opaque"       — not a GTIN at all (e.g. Code 128 SKU label): the
                         payload is its own identity, assigned by whoever
                         printed the label (us, a supplier, …).
      * "empty"        — no payload.
    """

    raw: str
    symbology: str
    kind: str
    key: str                    # canonical match key (gtin14 or raw payload)
    addon: str = ""             # stripped +2/+5 supplement, if any
    info: BarcodeInfo | None = None


def analyze_scan(barcode: str, symbology: str = "") -> ScanAnalysis:
    """Classify one scan record: number system, match key, global-uniqueness.

    `symbology` is the name reported by the scanner (docs §4.5); it guides
    interpretation but is not required — an unknown symbology with a valid
    GTIN payload is still recognised as a GTIN.
    """
    raw = (barcode or "").strip()
    if not raw:
        return ScanAnalysis(raw=raw, symbology=symbology, kind="empty", key="")

    symb = symbology.split("+")[0]
    base, addon = split_addon(raw, symbology)

    # An explicit opaque symbology (Code 128 SKU label, QR code, …) wins over
    # digit heuristics: its payload is an identifier chosen by whoever printed
    # the label, not a GS1 number — even when it would validate as a GTIN.
    # Unknown/empty symbology falls through to content-based detection.
    if symb not in OPAQUE_SYMBOLOGIES:
        gtin14 = normalize_gtin(raw, symbology)
        if gtin14 is not None:
            info = classify_gtin(gtin14, orig_len=len(base) if base.isdigit() else None)
            kind = "gtin" if info.globally_unique else "restricted"
            return ScanAnalysis(raw=raw, symbology=symbology, kind=kind,
                                key=gtin14, addon=addon, info=info)

        if base.isdigit() and len(base) in GTIN_LENGTHS:
            # Right shape, bad check digit: a misread EAN/UPC. Distinguish
            # from an opaque payload so intake never files this as a new
            # product.
            return ScanAnalysis(raw=raw, symbology=symbology, kind="invalid-gtin",
                                key="", addon=addon)

    # Not a GTIN: the payload is its own identity (e.g. a Code 128 SKU label).
    return ScanAnalysis(raw=raw, symbology=symbology, kind="opaque",
                        key=base, addon=addon)
