"""Third-party code vendored into the repository.

The project installs fully offline from a frozen uv.lock, so small pure-Python
dependencies are vendored instead of added to pyproject.toml:

- qrcodegen.py — Project Nayuki QR Code generator (MIT License),
  https://www.nayuki.io/page/qr-code-generator-library
  Verbatim copy; the license header is at the top of the file.
"""
