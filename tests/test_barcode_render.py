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
from io import BytesIO
from pathlib import Path

import pytest

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


def test_resolve_auto_disambiguates_a_valid_12_digit_upca():
    """036000291452 is a COMPLETE, valid UPC-A: auto must render exactly
    those digits. (The old behaviour appended an EAN-13 check digit and
    silently produced a different GTIN — a scannable barcode carrying the
    wrong number.)"""
    assert br.resolve("036000291452", "auto", force=False) == \
        ("036000291452", "upca")
    # explicit upca is unchanged: 12 digits verified as-is
    assert br.resolve("036000291452", "upca", force=False) == \
        ("036000291452", "upca")


def test_resolve_auto_keeps_ean13_body_when_upc_check_fails():
    """A 12-digit body that is NOT a valid UPC-A keeps the EAN-13 body
    interpretation (check digit appended) — the common retail case."""
    full, symbology = br.resolve("841234567890", "auto", force=False)
    assert (full, symbology) == ("841234567890" + check_digit("841234567890"),
                                 "ean13")


def test_resolve_round_trips_ean8_and_upca():
    # 7 digits -> EAN-8 body; the full code then passes through unchanged.
    assert br.resolve("1234567", "auto", force=False) == ("12345670", "ean8")
    assert br.resolve("12345670", "auto", force=False) == ("12345670", "ean8")
    # 11 digits -> UPC-A body; 12-digit full code (valid) -> UPC-A as-is.
    assert br.resolve("03600029145", "auto", force=False) == \
        ("036000291452", "upca")
    assert br.resolve("036000291452", "upca", force=False) == \
        ("036000291452", "upca")


def test_resolve_rejects_non_ascii_digits_before_the_bit_tables():
    """str.isdigit() alone accepts Arabic-Indic/fullwidth digits, which
    would then hit the ASCII-keyed tables as a raw KeyError traceback."""
    for bad in ("\u0661\u0662\u0663\u0664\u0665\u0666\u0667",          # ١٢٣٤٥٦٧
                "\uff14\uff10\uff10\uff16\uff13\uff18",                  # ４００６３８
                "12\u0663\u0664", "1234567\u0660"):
        with pytest.raises(ValueError):
            br.resolve(bad, "auto", force=True)  # --force must not bypass
        with pytest.raises(ValueError):
            br.resolve(bad, "ean13", force=True)


def test_svg_has_expected_dimensions_and_bar_count():
    bits = br.ean13_bits("8412345678905")
    svg = br._render_svg(bits, "8412345678905")
    # width = (2*quiet + 95) * module, module=3, quiet=11
    assert 'width="351"' in svg
    assert "8412345678905" in svg
    # 30 bar runs for this code (each run = one black <rect/>; the background
    # rect and the <text> label are not counted by 'fill="black"/>').
    assert svg.count('fill="black"/>') == 30


def test_check_mode_reports_invalid_instead_of_erroring(capsys):
    """A validator that can only ever print 'valid' is useless: --check on a
    wrong check digit must print the INVALID report (exit 1, report on
    stdout) instead of the bare error line."""
    rc = br.main(["8412345678906", "--check"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "symbology: ean13" in out
    assert "code:      8412345678906" in out
    assert "check:     6 (INVALID)" in out
    assert "expected 5" not in out  # the INVALID line carries it
    # without --check/--analyze it stays a one-line error on stderr
    capsys.readouterr()
    rc = br.main(["8412345678906"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "expected 5" in captured.err


def test_check_mode_reports_valid(capsys):
    rc = br.main(["8412345678905", "--check"])
    assert rc == 0
    assert "check:     5 (valid)" in capsys.readouterr().out


def test_check_with_format_writes_the_file(tmp_path, monkeypatch, capsys):
    """--format/--output must never be silently swallowed by recognition
    mode: only the flag-less default is print-only."""
    monkeypatch.chdir(tmp_path)
    rc = br.main(["036000291452", "--check", "--format", "svg"])
    assert rc == 0
    assert (tmp_path / "036000291452.svg").exists()
    assert "check:     2 (valid)" in capsys.readouterr().out
    # plain --check (no format, no output) stays print-only
    capsys.readouterr()
    rc = br.main(["036000291452", "--check"])
    assert rc == 0
    assert "print-only" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [tmp_path / "036000291452.svg"]


def test_svg_and_png_canvas_geometry_agree():
    """The two formats must be pixel-interchangeable: same canvas size, and
    the PNG's bars exactly `height` rows (PIL rectangles are inclusive)."""
    bits = br.ean13_bits("8412345678905")
    svg = br._render_svg(bits, "8412345678905", module=3, height=110)
    assert 'height="134"' in svg          # 110 + 24
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("Pillow not installed")
    png = br._render_png(bits, "8412345678905", module=3, height=110)
    img = Image.open(BytesIO(png))
    assert img.size == (351, 134)         # (2*11 + 95) * 3 wide, 110 + 24 tall
    # bar rows: the top-left quiet zone + first guard bar pixel is black,
    # the row at y=110 (the first label row) is white, and every row
    # between 0 and 109 in the guard column is black.
    px = img.load()
    guard_x = 11 * 3                      # first module after the quiet zone
    assert px[guard_x, 0] == (0, 0, 0)
    assert px[guard_x, 109] == (0, 0, 0)
    assert px[guard_x, 110] == (255, 255, 255)


def test_check_force_invalid_still_exits_nonzero(capsys):
    """--force lets an invalid code RENDER (misread testing), but --check
    must not report success for it: the report says INVALID and the exit
    code follows the verdict (round-2 review: it used to exit 0)."""
    rc = br.main(["8412345678906", "--check", "--force"])
    assert rc == 1
    assert "check:     6 (INVALID)" in capsys.readouterr().out
    # and with an explicit format the file IS written (that is --force's
    # purpose) — the exit code still reports the invalid verdict
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        import os
        old = os.getcwd()
        os.chdir(td)
        try:
            rc = br.main(["8412345678906", "--check", "--force",
                          "--format", "svg"])
            assert rc == 1
            assert (Path(td) / "8412345678906.svg").exists()
        finally:
            os.chdir(old)


def test_check_invalid_with_output_notifies_no_file(tmp_path, monkeypatch, capsys):
    """An explicitly requested file is never silently not written — not
    even on the invalid path (the same swallow class as --format)."""
    monkeypatch.chdir(tmp_path)
    rc = br.main(["8412345678906", "--check", "-o", "x.svg"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "check:     6 (INVALID)" in captured.out
    assert "no file written" in captured.err
    assert not (tmp_path / "x.svg").exists()


def test_output_extension_infers_format(tmp_path, monkeypatch, capsys):
    """--output label.png without --format must write PNG bytes (round-2
    review: it used to write SVG bytes into a .png filename). Explicit
    --format still wins over the extension."""
    monkeypatch.chdir(tmp_path)
    rc = br.main(["036000291452", "--output", "label.svg"])
    assert rc == 0
    assert (tmp_path / "label.svg").read_bytes().startswith(b"<svg")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("Pillow not installed")
    rc = br.main(["036000291452", "--output", "label.png"])
    assert rc == 0
    assert (tmp_path / "label.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # explicit --format beats the extension
    rc = br.main(["036000291452", "--output", "odd.png", "--format", "svg"])
    assert rc == 0
    assert (tmp_path / "odd.png").read_bytes().startswith(b"<svg")
    # unknown extension falls back to svg (the documented default)
    rc = br.main(["036000291452", "--output", "plain.txt"])
    assert rc == 0
    assert (tmp_path / "plain.txt").read_bytes().startswith(b"<svg")
    capsys.readouterr()
