# Spec: Localize application UI to Danish (da‑DK)

Status: **SUPERSEDED (2026-11-01)** — kept for history. The delivered implementation differs on the
language-switcher, message-catalogue, currency and brand decisions; see
`docs/danish-terminology.md` §"Relationship to the older localization spec" for the authoritative
comparison, and `i18n/README.md` for the workflow that replaced this plan.
Originally: Approved — ready to implement.
Owner: omp + tester/tester2 agents.
Date: 2026-10-20.

## 1. Goal

Change every **user‑facing** string in the **SPA** from English to Danish.
Single‑language change: Danish replaces English. No language switcher.

## 2. Non-goals / out of scope

- No runtime language selection, no reactivation of English.
- **Backend API text stays English** (decision B): `HTTPException.detail`,
  Pydantic messages, and OpenAPI summaries/descriptions are unchanged.
- No change to machine‑readable identifiers: API field names, URL paths, query
  params, enum values, log output, docstrings, code comments, internal error codes.
- No automatic translation of user‑supplied content (item names entered by users
  stay as entered).
- Env var names (`HCRM_*`), the DB filename `hcrm.db`, repo name, and code
  identifiers keep the `HCRM`/English form (machine-readable, decision C §10).

## 3. String surface (verified on `main`)

| Source | Location | In scope | Notes |
|---|---|---|---|
| SPA markup | `static/index.html` (1033 ln) | ✅ | Nav, forms, buttons, headings, empty states, `<title>` |
| SPA logic | `static/app.js` (2449 ln), `static/scancapture.js`, `static/scanfeed.js` | ✅ | Inline template strings, alerts, toasts |
| Backend user errors | `app/routers/*.py`, `app/schemas.py`, `app/deps.py` | ❌ | Keep English (decision B) |
| Seed data | `app/seed.py` | ✅ | Product names & descriptions → Danish (decision A) |
| Printed/QR output | `app/qrbadge.py`, `app/barcodes.py` | verify | Translate only human‑readable label text, not machine data |

## 4. Approach

In-place translation, one language. No i18n framework, no message catalog: keep
the change reviewable and avoid abstraction the codebase doesn't need.

- Frontend: translate every literal shown in the SPA directly in `index.html`
  and the JS files.
- Seed data: translate `name`/`description` for the demo catalog. Fresh seeds
  only; existing DB rows are not migrated (documented).
- Since the API stays English, the SPA **must not** render raw `detail`/message
  strings from error responses verbatim where they are user‑visible; map known
  error conditions to Danish UI text instead.

Alternative considered and rejected: `vue-i18n` / Flask‑Babel + `.po` catalogs.
Rejected because there is a single target language and no requirement to switch;
a catalog layer would be dead weight in a 3.9 k‑line SPA.

## 5. Danish conventions (apply throughout)

- Locale `da‑DK`.
- Dates: `dd.mm.yyyy` (or `d. mmmm yyyy` for long forms). Numbers: comma decimal
  separator (e.g. `123,45`), dot thousands separator. Currency: `kr. / DKK`,
  right‑aligned like existing UI.
- Use contemporary Danish UI wording: "Log ind", "Opret konto", "Kurv",
  "Katalog", "Butik", "Kasse" / "Gennemfør køb", "Profil", "Log ud", "Søg".
- A single `docs/danish-terminology.md` glossary MUST be created and used, so
  identical concepts get identical translations across the SPA and labels.

## 6. Acceptance criteria

1. Fresh `uv run pytest -q` passes (see §8 for expected test edits).
2. Running the app: every visible SPA screen renders Danish with no English
   leftovers (spot-check: login, signup, catalogue, table view, cart, admin,
   profile, scan feed).
3. Fresh seed produces Danish demo catalog.
4. Dates/numbers/currency follow §5.
5. No English remains in `static/` UI literals or Danish‑surfaced badge/label
   text; `grep` proof provided (list of files, counts).
6. SPA never surface raw English API error text to users (mapped to Danish).

## 7. Risks

- **Layout overflow**: Danish words are longer (e.g. "Gennemfør køb" vs
  "Complete purchase"). Check button/table widths; adjust CSS only where a
  rendering break is observed.
- **Test coupling**: tests currently assert English strings
  (`test_auth.py`, `test_purchases.py`, `test_logoff.py` found by grep). These
  MUST be updated to the Danish equivalents in the same change; do not weaken or
  delete meaningful assertions.
- **Legacy content**: existing DB rows keep English demo items after update
  (§4). Noted, not migrated.
- **Concurrent work**: item 1 (slow‑test fix) is running in the `tester` pane
  now and may touch `tests/`. Rebase/check `git status` before touching tests.

## 8. Implementation plan

1. Create `docs/danish-terminology.md` glossary.
2. Translate `static/index.html` and `static/*.js`.
3. Translate `app/seed.py` catalog.
4. Translate human‑readable badge/label text if present.
5. Update affected tests to Danish expectations.
6. Run full suite + manual SPA smoke (browser, non‑blind detector from W2.3).
7. Update this spec to `Status: implemented` with evidence.

## 9. Decisions (user-confirmed 2026-10-20)

- A. **Seed catalog: translate.** `app/seed.py` demo names/descriptions → Danish.
  Fresh seeds only; existing DB rows are not migrated.
- B. **API text: keep English.** Backend `detail`, Pydantic, and OpenAPI
  summaries stay English. Only the SPA strings are localized.
- C. **Brand: rename** HCRM → **"Lageret"** (selected 2026-10-20).

## 10. Brand name (selected)

**Lageret** — "the stockroom" / "the store". User-selected.

Rename scope: UI header/logo, `<title>`, README intro line, and any user-visible
occurrences of "HCRM". **Env var names (`HCRM_*`), the DB filename `hcrm.db`,
repo name, and code identifiers stay unchanged** (machine-readable).
