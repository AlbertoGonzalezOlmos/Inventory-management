#!/usr/bin/env python3
"""barcode_render — render / recognise GS1 retail barcodes (EAN-13, EAN-8, UPC-A).

The repo already understands the barcode *numbering system* (``app/barcodes.py``
— GTIN validation, normalisation, classification; see ``docs/barcodes.md``) but
had nothing that renders the physical *symbology*, so a hardware check had
nothing scannable but a QR stand-in. This script closes that gap: it encodes
the digits into the bar/space patterns of the GS1 retail symbologies and
renders them as a scannable **SVG** (no dependencies) or **PNG** (needs
Pillow). It also *recognises* a code: check digit + the same GS1
classification the API uses (``analyze_scan``).

Symbology vs number system (``docs/barcodes.md`` §1): the bars here are the
physical layer; the digits they carry are GTINs from the GS1 number space.
The check digit is the GS1 modulo-10 (weights 3,1,3,1,… from the right) —
the exact ``app.barcodes.check_digit``, reused so this script can never drift
from the API's own validation.

Encoding tables: only the L (left, odd parity) patterns are transcribed from
the GS1 spec; the G (left, even) and R (right) patterns are DERIVED from L
(the spec's own identities: R = bitwise complement of L, G = reverse of R),
so there is one source table to trust and it is pinned by a property test
(``tests/test_barcode_render.py``).

Usage:
  scripts/barcode_render.py 841234567890            # EAN-13: computes check
  scripts/barcode_render.py 8412345678905 --check   # validate the check digit
  scripts/barcode_render.py 8412345678905 --analyze # GS1 classification
  scripts/barcode_render.py 841234567890 --format png --output barcode.png
"""

from __future__ import annotations

import argparse
import os
import sys

# repo root on sys.path so `app.barcodes` imports (the scripts/ convention).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.barcodes import analyze_scan, check_digit  # noqa: E402

# The ONE transcribed table: EAN-13 left-hand "odd parity" (L) patterns,
# 7 bits each, 1 = bar, 0 = space (GS1 General Specifications / ISO 15420).
_L = {
    "0": "0001101",
    "1": "0011001",
    "2": "0010011",
    "3": "0111101",
    "4": "0100011",
    "5": "0110001",
    "6": "0101111",
    "7": "0111011",
    "8": "0110111",
    "9": "0001011",
}


def _complement(bits: str) -> str:
    return "".join("1" if b == "0" else "0" for b in bits)


def _reverse(bits: str) -> str:
    return bits[::-1]


# R (right, even parity) = bitwise complement of L.
_R = {d: _complement(bits) for d, bits in _L.items()}
# G (left, even parity) = reverse of R (equivalently reverse-complement of L).
_G = {d: _reverse(bits) for d, bits in _R.items()}

# EAN-13 first digit -> parity of the six left-hand digits (L=odd, G=even).
_PARITY = {
    "0": "LLLLLL",
    "1": "LLGLGG",
    "2": "LLGGLG",
    "3": "LLGGGL",
    "4": "LGLLGG",
    "5": "LGGLLG",
    "6": "LGGGLL",
    "7": "LGLGLG",
    "8": "LGLGGL",
    "9": "LGGLGL",
}

_START = "101"
_CENTER = "01010"
_END = "101"


def ean13_bits(code13: str) -> str:
    """Encode a 13-digit EAN-13 into its 95-module bar/space bit string."""
    if len(code13) != 13 or not code13.isdigit():
        raise ValueError("EAN-13 needs exactly 13 digits")
    first, left, right = code13[0], code13[1:7], code13[7:]
    parity = _PARITY[first]
    bits = _START
    for d, p in zip(left, parity):
        bits += (_L if p == "L" else _G)[d]
    bits += _CENTER
    for d in right:
        bits += _R[d]
    return bits + _END


def ean8_bits(code8: str) -> str:
    """Encode an 8-digit EAN-8 (4 L digits + 4 R digits) into 67 modules."""
    if len(code8) != 8 or not code8.isdigit():
        raise ValueError("EAN-8 needs exactly 8 digits")
    bits = _START
    for d in code8[:4]:
        bits += _L[d]
    bits += _CENTER
    for d in code8[4:]:
        bits += _R[d]
    return bits + _END


def upca_bits(code12: str) -> str:
    """Encode a 12-digit UPC-A (6 L digits + 6 R digits) into 95 modules."""
    if len(code12) != 12 or not code12.isdigit():
        raise ValueError("UPC-A needs exactly 12 digits")
    bits = _START
    for d in code12[:6]:
        bits += _L[d]
    bits += _CENTER
    for d in code12[6:]:
        bits += _R[d]
    return bits + _END


def _bits_for(code: str, symbology: str) -> str:
    if symbology == "ean13":
        return ean13_bits(code)
    if symbology == "ean8":
        return ean8_bits(code)
    if symbology == "upca":
        return upca_bits(code)
    raise ValueError(f"unknown symbology {symbology!r}")


def _render_svg(bits: str, label: str, *, module: int = 3,
                height: int = 110, quiet: int = 11) -> str:
    """Render a bit string as a scannable SVG (one black rect per bar run)."""
    width = (quiet * 2 + len(bits)) * module
    label_y = height + 16
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{label_y + 8}" viewBox="0 0 {width} {label_y + 8}">',
        f'<rect width="{width}" height="{label_y + 8}" fill="white"/>',
    ]
    # Merge consecutive bars into single rects (runs of '1') for clean output.
    x = quiet * module
    i = 0
    while i < len(bits):
        if bits[i] == "1":
            j = i
            while j < len(bits) and bits[j] == "1":
                j += 1
            out.append(
                f'<rect x="{x}" y="0" width="{(j - i) * module}" '
                f'height="{height}" fill="black"/>'
            )
            x += (j - i) * module
            i = j
        else:
            x += module
            i += 1
    if label:
        out.append(
            f'<text x="{width / 2}" y="{label_y}" text-anchor="middle" '
            f'font-family="monospace" font-size="{min(20, module * 6)}" '
            f'fill="black">{label}</text>'
        )
    out.append("</svg>")
    return "\n".join(out)


def _render_png(bits: str, label: str, *, module: int = 3,
                height: int = 110, quiet: int = 11) -> bytes:
    """Render a bit string as a PNG (needs Pillow; import lazily)."""
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on host
        raise SystemExit("PNG output needs Pillow (`pip install pillow`); "
                         "use --format svg instead") from exc
    width = (quiet * 2 + len(bits)) * module
    img = Image.new("RGB", (width, height + 28), "white")
    draw = ImageDraw.Draw(img)
    x = quiet * module
    i = 0
    while i < len(bits):
        if bits[i] == "1":
            j = i
            while j < len(bits) and bits[j] == "1":
                j += 1
            draw.rectangle([x, 0, x + (j - i) * module - 1, height],
                           fill="black")
            x += (j - i) * module
            i = j
        else:
            x += module
            i += 1
    if label:
        try:
            font = ImageFont.load_default()
        except Exception:  # pragma: no cover
            font = None
        draw.text((width // 2, height + 6), label, fill="black",
                  anchor="ma", font=font)
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def resolve(code: str, symbology: str, force: bool) -> tuple[str, str]:
    """Normalise a digit string to the full code for a symbology.

    Returns ``(full_code, symbology)``. A short body has its check digit
    computed; a full code is verified (unless ``force``).
    """
    body = code.strip()
    if not body.isdigit():
        raise ValueError("barcode digits must be 0-9 only")
    if symbology == "auto":
        if len(body) == 7 or len(body) == 8:
            symbology = "ean8"
        elif len(body) == 11:
            symbology = "upca"
        elif len(body) in (12, 13):
            symbology = "ean13"
        else:
            raise ValueError(
                f"cannot infer a symbology from {len(body)} digits — "
                "use --symbology (ean13/ean8/upca)")
    sizes = {"ean8": 8, "upca": 12, "ean13": 13}
    full = sizes[symbology]
    if len(body) == full - 1:
        return body + check_digit(body), symbology
    if len(body) == full:
        if not force and check_digit(body[:-1]) != body[-1]:
            raise ValueError(
                f"check digit {body[-1]} is wrong for {body[:-1]} "
                f"(expected {check_digit(body[:-1])}) — re-check, or pass "
                "--force to render the invalid code as-is (misread testing)")
        return body, symbology
    raise ValueError(f"{symbology} needs {full - 1} or {full} digits, "
                     f"got {len(body)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("code", help="digits (short body or full code)")
    parser.add_argument("--symbology", choices=["auto", "ean13", "ean8", "upca"],
                        default="auto",
                        help="symbology to encode (default: infer from length)")
    parser.add_argument("--format", choices=["svg", "png"], default="svg")
    parser.add_argument("--output", "-o", help="output file (default: <code>.<fmt>)")
    parser.add_argument("--module", type=int, default=3, help="bar module width (px)")
    parser.add_argument("--height", type=int, default=110, help="bar height (px)")
    parser.add_argument("--no-label", action="store_true", help="omit the digits under the bars")
    parser.add_argument("--check", action="store_true", help="validate + print the check digit")
    parser.add_argument("--analyze", action="store_true", help="print the GS1 classification")
    parser.add_argument("--force", action="store_true",
                        help="render a wrong-check-digit code as-is (misread testing)")
    args = parser.parse_args(argv)

    try:
        full, symbology = resolve(args.code, args.symbology, args.force)
    except ValueError as exc:
        print(f"barcode_render: {exc}", file=sys.stderr)
        return 1

    if args.check or args.analyze:
        print(f"symbology: {symbology}")
        print(f"code:      {full}")
        print(f"check:     {full[-1]} "
              f"({'valid' if check_digit(full[:-1]) == full[-1] else 'INVALID'})")
        if args.analyze:
            a = analyze_scan(full, symbology="EAN-13" if symbology == "ean13"
                             else ("EAN-8" if symbology == "ean8" else "UPC-A"))
            print(f"kind:      {a.kind}")
            print(f"key:       {a.key}")
            if a.info is not None:
                print(f"format:    {a.info.format}")
                print(f"region:    {a.info.region or '-'}")
                print(f"global:    {a.info.globally_unique}")
    if args.output is None and (args.check or args.analyze):
        return 0  # recognition modes: print only, no file unless --output given

    bits = _bits_for(full, symbology)
    label = "" if args.no_label else full
    if args.format == "svg":
        payload = _render_svg(bits, label, module=args.module,
                              height=args.height).encode()
    else:
        payload = _render_png(bits, label, module=args.module,
                              height=args.height)

    out = args.output or f"{full}.{args.format}"
    with open(out, "wb") as f:
        f.write(payload)
    print(f"barcode_render: wrote {out} ({symbology} {full}, "
          f"{len(bits)} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
