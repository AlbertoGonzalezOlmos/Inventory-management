"""The stable error-code registry (item 6 of the Danish-UI roadmap).

Every error response the SPA can *display* now answers with an additive
``X-Error-Code`` response header. The body is untouched: a plain-string detail
stays the same English sentence it has always been (the existing
``detail == "..."`` assertions are part of the wire contract), and a
structured ``detail={"status": ...}`` envelope keeps its shape and its message.

Why a registry
--------------
The code and the English message live in ONE table, so a site cannot raise
``"Email already registered"`` under a code whose message says something else
— the site asks for the code and gets both. The generated copy source
(``i18n/strings.csv``) carries one ``error.<code>`` row per entry (en = the
message below, da = the translation), which is what lets the browser render
the same condition in the UI language instead of the server's English.

Two site shapes
---------------
* :func:`api_error` — a plain-string detail. The body is exactly
  ``{"detail": <registry message with its vars>}``; the header carries the code
  and, for a parameterised message, the values (``X-Error-Vars``, URL-encoded
  so a non-ASCII value cannot break the header encoding).
* :func:`coded_detail` — a structured envelope whose body must not change. It
  attaches the same headers and leaves ``detail`` to the caller (which reads
  its message from :data:`ERRORS`, so the copy still cannot drift).

What is deliberately NOT coded
------------------------------
* ``scan_unknown`` (cart 404): its message IS the scan verdict
  (``app.scanlookup.verdict_message``), generated per scan and deliberately
  identical to the bridge's stdout wording — not registry copy.
* ``scan_request_not_open`` (409): its message embeds a machine status value
  ("request is already resolved"), whose visible label would need a nested
  lookup inside the sentence.
* the scanner feed's ``_AUTH_FAILED`` (`app.routers.scanner`): a daemon
  credential failure, never displayed as UI copy.
Those keep their exact bodies and the machine ``status`` discriminator the SPA
already branches on; ``errorText`` still falls back to the raw message.

HTTP status is part of the entry (the registry is also what the tests drive),
but nothing here formats a response: only ``headers`` is added to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import HTTPException

#: The additive response header. A bare machine code (``lower_snake``), never
#: a UI id: the wire contract must not name a CSV row.
HEADER_CODE = "X-Error-Code"
#: The optional companion header for a parameterised message: the values, as a
#: URL-encoded query string (sorted, so the header is deterministic).
HEADER_VARS = "X-Error-Vars"


@dataclass(frozen=True)
class Error:
    """One stable code: its HTTP status and its English message template."""

    code: str
    status: int
    message: str
    #: UI wording when it must differ from the body (the body is frozen and may
    #: carry a legacy shape the copy rules forbid — see ``cart_items_missing``).
    copy: str | None = None

    @property
    def text(self) -> str:
        """The English copy the UI should show (falls back to the body)."""
        return self.copy if self.copy is not None else self.message


_REGISTRY: tuple[Error, ...] = (
    # --- auth guards (app.deps) ------------------------------------------------
    Error("not_authenticated", 401, "Not authenticated"),
    Error("token_invalid", 401, "Token invalid or expired"),
    Error("user_gone", 401, "User no longer exists"),
    Error(
        "password_change_required", 403,
        "Password change required: change your password "
        "(Account → Change password) before using the app",
    ),
    Error("staff_required", 403, "Staff privileges required"),
    Error("admin_required", 403, "Admin privileges required"),
    # --- auth: register, login, badge, profile ---------------------------------
    Error(
        "well_known_password", 400,
        "That password is published in this project's documentation/seed code; "
        "choose another one",
    ),
    Error("email_taken", 409, "Email already registered"),
    Error("invalid_credentials", 401, "Invalid email or password"),
    Error("invalid_badge", 401, "Invalid QR badge"),
    Error("current_password_incorrect", 400, "Current password is incorrect"),
    Error("password_unchanged", 400,
          "New password must differ from the current password"),
    Error("email_unchanged", 400,
          "New email must differ from the current email"),
    # Badge VIEW envelopes (structured): the staff badge view surfaces these raw.
    Error("badge_none", 404,
          "No QR badge for this account — generate one first"),
    Error("badge_no_key", 409,
          "Badge re-viewing is unavailable (no derivation "
          "key) — logins are unaffected; regenerate the "
          "badge once a key exists"),
    Error("badge_key_mismatch", 409,
          "The badge key changed since this badge was "
          "issued — regenerate the badge to re-enable "
          "viewing (logins are unaffected: they verify "
          "by hash)"),
    # --- cart ------------------------------------------------------------------
    Error("item_not_found", 404, "Item not found"),
    Error("cart_line_missing", 404, "Item not in cart"),
    Error("cart_duplicate_lines", 422,
          "Duplicate item_id in lines: {duplicates}"),
    # The body keeps the legacy "Item(s)" shape (its assertions are part of the
    # wire contract); the copy rules forbid the "(s)" idiom, so the UI row uses
    # the plural-only wording.
    Error("cart_items_missing", 404, "Item(s) not found: {missing}",
          copy="Items not found: {missing}"),
    Error("scan_badge", 400,
          "QR badge payload — badges log in via "
          "POST /api/auth/qr-login, they are not "
          "catalogue items"),
    Error("scan_misread", 422,
          "MISREAD — check digit fails; re-scan, do not file"),
    # --- items -----------------------------------------------------------------
    Error("invalid_gtin", 422,
          "not a valid GTIN (bad check digit or length): {barcode}"),
    Error("offset_requires_limit", 422,
          "offset requires a limit (omit limit to rank every item)"),
    Error("vector_model_unavailable", 503,
          "Vector search unavailable (embedding model not loaded)"),
    Error("vector_extension_unavailable", 503,
          "Vector search unavailable (sqlite-vector extension not loaded)"),
    Error("sku_taken", 409, "SKU already exists"),
    Error("barcode_taken", 409, "Barcode already assigned to item {sku}"),
    Error("embedding_unavailable_create", 503,
          "Embedding model unavailable; cannot create items right now "
          "(they would be invisible to semantic search). Try again shortly."),
    Error("item_conflict_create", 409,
          "SKU or barcode already exists (concurrent create)"),
    Error("embedding_unavailable_update", 503,
          "Embedding model unavailable; cannot change item name or "
          "description right now. No changes were saved."),
    Error("barcode_taken_concurrent", 409,
          "Barcode already assigned to another item (concurrent update)"),
    Error("stock_db_locked", 503,
          "database is locked by another writer; retry the adjustment"),
    # --- orders ----------------------------------------------------------------
    Error("cart_empty", 400, "Cart is empty"),
    Error("checkout_db_locked", 503,
          "database is locked by another writer; retry the checkout"),
    # --- members (staff/admin account management) ------------------------------
    Error("invalid_role", 400, "Invalid role"),
    Error("admin_create_forbidden", 403,
          "Only admins can create admin accounts"),
    Error("member_not_found", 404, "Member not found"),
    Error("admin_modified_forbidden", 403,
          "Admin accounts can only be modified by admins"),
    Error("admin_role_grant_forbidden", 403,
          "Only admins can grant the admin role"),
    Error("last_admin_demote", 400, "Cannot demote the last admin"),
    Error("admin_password_reset_forbidden", 403,
          "Only admins can reset passwords"),
    Error("use_change_password", 400,
          "Use POST /api/auth/change-password to change your own password "
          "(it verifies your current password)"),
    Error("admin_managed_forbidden", 403,
          "Admin accounts can only be managed by admins"),
    Error("self_delete", 400, "Cannot delete yourself"),
    Error("last_admin_delete", 400, "Cannot delete the last admin"),
    # --- scan requests (the counter's unknown-item queue) ----------------------
    Error("scan_request_not_found", 404, "Scan request not found"),
    Error("scan_request_status_invalid", 422,
          "status must be one of {statuses}"),
    Error("intake_already", 409, "This code is already in the catalogue"),
    Error("intake_duplicate", 409,
          "You already have an open request for this code"),
    # --- scanner: the kiosk pairing surface (structured envelopes) -------------
    Error("register_loopback_only", 403,
          "Terminal registration is loopback-only by "
          "default (HCRM_SCANNER_REGISTER_ANY=1 to open "
          "for a LAN deployment)"),
    Error("terminal_already_registered", 409,
          "Terminal {terminal} is already "
          "registered — recovery is DELETE "
          "/api/scanner/terminals/{terminal_url} (staff or loopback), "
          "then re-register"),
    Error("secret_in_use", 409,
          "That secret is already registered to "
          "terminal {owner} — "
          "generate a fresh secret per terminal"),
    Error("pair_loopback_only", 403,
          "One-click pairing is loopback-only by "
          "default (HCRM_SCANNER_PAIR_ANY=1 to open "
          "for a LAN deployment)"),
    Error("bad_pairing", 401, "Unknown terminal or wrong pairing code"),
    Error("already_paired", 409,
          "This terminal is already paired — DELETE it "
          "(staff or loopback) and re-register to "
          "re-pair"),
    Error("json_required", 415, "Content-Type: application/json is required"),
    Error("no_pairing_file", 404,
          "No pairing file on this host — run "
          "scanner_daemon.py --register first"),
    Error("pairing_file_unreadable", 500,
          "The pairing file {path} exists but cannot "
          "be read — the daemon and the server must "
          "run as the same user (systemd User=), or "
          "set HCRM_SCANNER_PAIRING_FILE"),
    Error("bad_pairing_file", 422,
          "The pairing file is malformed — regenerate "
          "it with scanner_daemon.py --register"),
    Error("terminal_staff_required", 403,
          "Deleting a terminal requires staff "
          "privileges (or a loopback caller)"),
    Error("terminal_not_found", 404, "No terminal {terminal}"),
    # --- framework-level envelopes (app.main's exception handlers) -------------
    # FastAPI's own request validation (422). Two codes: a body field is named
    # in ``detail[0].loc`` and gets its label row; a model-level error (loc
    # ["body"] only) gets the generic sentence. The body is unchanged — the
    # handler delegates to FastAPI's own and only adds the header.
    Error("validation", 422, "Check the highlighted fields"),
    Error("validation_field", 422, "{field} is not valid"),
    Error("internal_error", 500, "Internal server error"),
)

#: code -> entry. The single source both the routers and the tests read.
ERRORS: dict[str, Error] = {error.code: error for error in _REGISTRY}
assert len(ERRORS) == len(_REGISTRY), "duplicate error code in the registry"


def spec(code: str) -> Error:
    """The registry entry for ``code`` (KeyError on an unregistered code —
    a typo must fail at import/raise time, never ship a codless response)."""
    return ERRORS[code]


def validation_code(errors: list[dict]) -> str:
    """Which 422 code a list of pydantic errors deserves (field-named or not)."""
    for error in errors:
        loc = error.get("loc") or ()
        if len(loc) >= 2 and isinstance(loc[-1], str) and loc[-1].isidentifier():
            return "validation_field"
    return "validation"


def code_headers(code: str, vars: dict[str, object] | None = None,
                 headers: dict[str, str] | None = None) -> dict[str, str]:
    """The additive headers for a coded error (merging any the site already
    carries, e.g. ``Retry-After`` on the 503s)."""
    merged = dict(headers or {})
    merged[HEADER_CODE] = spec(code).code
    if vars:
        merged[HEADER_VARS] = urlencode(
            [(name, str(value)) for name, value in sorted(vars.items())]
        )
    return merged


def api_error(code: str, *, headers: dict[str, str] | None = None,
              **vars: object) -> HTTPException:
    """A plain-string error: body = the registry message, plus the code header.

    ``**vars`` fills the message template (and is published in
    ``X-Error-Vars`` so the browser can render the same sentence in Danish).
    """
    entry = spec(code)
    return HTTPException(
        status_code=entry.status,
        detail=entry.message.format(**vars) if vars else entry.message,
        headers=code_headers(code, vars, headers),
    )


def coded_detail(code: str, detail: dict, *,
                 headers: dict[str, str] | None = None,
                 **vars: object) -> HTTPException:
    """A structured-detail error: the body is the caller's dict, untouched.

    The dict's ``message`` should be ``ERRORS[code].message`` (formatted with
    ``vars``) so the wire copy and the registry cannot drift; ``vars`` here is
    only what travels in the header for the browser's row.
    """
    return HTTPException(
        status_code=spec(code).status,
        detail=detail,
        headers=code_headers(code, vars, headers),
    )
