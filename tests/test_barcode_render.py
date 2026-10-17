"""Contract pins for scripts/barcode_render.py (the physical GS1 symbology).

``app/barcodes.py`` already owns (and is tested for) the barcode *number
system*. This suite pins the new *physical encoding* layer so a transcribed
bar table can never silently drift.

The ground truth here is the GS1/ISO 15420 tables transcribed as literal
values: L (left, odd parity), R (right, even), G (left, even), and the
EAN-13 first-digit PARITY table. R and G are *derived* in the script from L
(the spec's identities: R = complement(L), G = reverse(R)); the derivation is
checked separately, but the RESULT is pinned against hard-coded spec values
so a broken derive step (e.g. a wrong complement) cannot pass on its own
tautology. A full known-answer bit string then pins the encode *logic*.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.barcode_render as br  # noqa: E402
from app.barcodes import check_digit  # noqa: E402


def test_l_table_is_the_gs1_odd_parity_encodings():
    expected = {
        "0": "0001101", "1": "0011001", "2": "0010011", "3": "0111101",
        "4": "0100011", "5": "0110001", "6": "0101111", "7": "0111011",
        "8": "0110111", "9": "0001011",
    }
    assert br._L == expected


def test_r_table_is_the_gs1_right_even_parity_encodings():
    expected = {
        "0": "1110010", "1": "1100110", "2": "1101100", "3": "1000010",
        "4": "1011100", "5": "1001110", "6": "1010000", "7": "1000100",
        "8": "1001000", "9": "1110100",
    }
    assert br._R == expected


def test_g_table_is_the_gs1_left_even_parity_encodings():
    expected = {
        "0": "0100111", "1": "0110011", "2": "0011011", "3": "0100001",
        "4": "0011101", "5": "0111001", "6": "0000101", "7": "0010001",
        "8": "0001001", "9": "0010111",
    }
    assert br._G == expected


def test_r_and_g_derive_identities_hold():
    """R = complement(L), G = reverse(R) — the spec's own identities."""
    for d in "0123456789":
        assert br._R[d] == br._complement(br._L[d])
        assert br._G[d] == br._reverse(br._R[d])


def test_parity_table_matches_the_gs1_first_digit_table():
    expected = {
        "0": "LLLLLL", "1": "LLGLGG", "2": "LLGGLG", "3": "LLGGGL",
        "4": "LGLLGG", "5": "LGGLLG", "6": "LGGGLL", "7": "LGLGLG",
        "8": "LGLGGL", "9": "LGGLGL",
    }
    assert br._PARITY == expected


def test_ean13_structure():
    bits = br.ean13_bits("8412345678905")
    assert len(bits) == 95
    assert bits[:3] == "101" and bits[-3:] == "101"
    assert bits[45:50] == "01010"          # centre guard, exact middle
    assert set(bits) <= {"0", "1"}


def test_ean8_and_upca_structure():
    assert len(br.ean8_bits("12345670")) == 67
    assert br.ean8_bits("12345670")[31:36] == "01010"
    assert len(br.upca_bits("123456789012")) == 95
    assert br.upca_bits("123456789012")[45:50] == "01010"


def test_ean13_known_answer_bit_string():
    """A complete known-answer vector (Stabilo 4006381333931, docs/barcodes.md).

    Pins the encode LOGIC — given the (separately pinned) L/R/G/PARITY tables,
    a wrong digit order, wrong parity pick, or misplaced guard would break
    this exact 95-module string.
    """
    assert br.ean13_bits("4006381333931") == (
        "1010001101010011101011110111101000100101100110"
        "1010100001010000101000010111010010000101100110101"
    )


def test_resolve_computes_and_verifies_check_digit():
    # 12 digits = EAN-13 body → the check digit is appended, and it agrees
    # with the repo's own app.barcodes.check_digit (never drift).
    full, symbology = br.resolve("841234567890", "auto", force=False)
    assert symbology == "ean13"
    assert full == "841234567890" + check_digit("841234567890")
    # A full valid code passes through unchanged.
    assert br.resolve("8412345678905", "auto", force=False) == \
        ("8412345678905", "ean13")


def test_resolve_rejects_a_bad_check_digit_unless_forced():
    try:
        br.resolve("8412345678906", "auto", force=False)
    except ValueError as exc:
        assert "expected 5" in str(exc)
    else:
        raise AssertionError("a wrong check digit must be rejected")
    # --force is the misread-testing escape hatch.
    assert br.resolve("8412345678906", "auto", force=True) == \
        ("8412345678906", "ean13")


def test_resolve_upca_requires_explicit_symbology():
    """A 12-digit input is ambiguous; auto resolves it to EAN-13, and a real
    UPC-A must be requested explicitly (the script also warns on stderr)."""
    # auto treats 12 digits as an EAN-13 body: check digit appended -> 13.
    assert br.resolve("036000291452", "auto", force=False) == \
        ("0360002914522", "ean13")
    # explicit upca keeps the 12 digits as-is (check digit verified).
    assert br.resolve("036000291452", "upca", force=False) == \
        ("036000291452", "upca")


def test_svg_has_expected_dimensions_and_bar_count():
    bits = br.ean13_bits("8412345678905")
    svg = br._render_svg(bits, "8412345678905")
    # width = (2*quiet + 95) * module, module=3, quiet=11
    assert 'width="351"' in svg
    assert "8412345678905" in svg
    # 30 bar runs for this code (each run = one black <rect/>; the background
    # rect and the <text> label are not counted by 'fill="black"/>').
    assert svg.count('fill="black"/>') == 30
