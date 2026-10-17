"""Contract pins for scripts/barcode_render.py (the physical GS1 symbology).

``app/barcodes.py`` already owns (and is tested for) the barcode *number
system*. This suite pins the new *physical encoding* layer so a transcribed
bar table can never silently drift. The one authoritative table is L (left,
odd parity); R and G are derived from it by the spec's own identities
(R = bitwise complement of L, G = reverse of R), and that derivation is
pinned here — so a corrupted L, a stray "helpful" edit, or a broken derive
step each fail loudly.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.barcode_render as br  # noqa: E402
from app.barcodes import check_digit  # noqa: E402


def test_l_table_is_the_gs1_odd_parity_encodings():
    """The transcribed L patterns (GS1 General Specifications / ISO 15420)."""
    expected = {
        "0": "0001101", "1": "0011001", "2": "0010011", "3": "0111101",
        "4": "0100011", "5": "0110001", "6": "0101111", "7": "0111011",
        "8": "0110111", "9": "0001011",
    }
    assert br._L == expected


def test_r_and_g_are_derived_from_l():
    """R = complement(L), G = reverse(R) — the spec's own identities."""
    for d in "0123456789":
        assert br._R[d] == br._complement(br._L[d])
        assert br._G[d] == br._reverse(br._R[d])


def test_parity_table_covers_all_first_digits():
    assert set(br._PARITY) == set("0123456789")
    for pat in br._PARITY.values():
        assert len(pat) == 6 and set(pat) <= {"L", "G"}


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


def test_svg_has_expected_dimensions_and_a_label():
    bits = br.ean13_bits("8412345678905")
    svg = br._render_svg(bits, "8412345678905")
    # width = (2*quiet + 95) * module, module=3, quiet=11
    assert 'width="351"' in svg
    assert "8412345678905" in svg
    # one black rect per bar run (the start guard alone is two runs)
    assert svg.count('fill="black"') >= 2
