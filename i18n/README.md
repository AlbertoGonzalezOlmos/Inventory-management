# i18n copy source

`strings.csv` is the **only** hand-edited source of user-visible copy. The app never
reads it: `scripts/build_i18n.py` compiles it into `static/i18n.messages.js`, which is
served and committed. **Danish is the default UI language** (`DEFAULT_LANG` in
`static/i18n.js`): a fresh device boots Danish, the toggle or `?lang=en` switches to
English, and English remains the fallback for a row missing from `da`.

## Loop

1. Edit `i18n/strings.csv` (UTF-8 **with BOM**, comma-separated, one row per string).
2. `uv run python scripts/build_i18n.py --write`
3. Commit **both** files together.
4. `uv run python scripts/build_i18n.py --check` verifies freshness; `scripts/run.sh` and
   CI refuse to serve a stale artifact. `--report` prints counts. Nothing ever writes the
   CSV for you.

## Rules the build enforces

- Header exactly `id,en,da`; every row has three cells; ids sorted (the build never
  re-sorts). Id grammar: `lower.snake`, at least one dot, e.g. `checkout.review_title`.
  Quoting is strict: an undoubled `"` inside a quoted cell fails the build (it is
  never silently dropped).
- No empty cells (whitespace-only counts as empty), no leading or trailing whitespace,
  no embedded newlines, no control characters (tab/NUL/ESC/…), no `<`/`>` markup, no
  euro sign, no BOM inside a cell, no unbalanced or malformed `{token}`.
- `{name}` placeholders must appear in **both** languages as the same **multiset** —
  repeat counts included (`{n} {n}` ≠ `{n}`).
- Plurals are the sibling pair `<base>.one` / `<base>.other`, both present and
  agreeing on the same placeholders.
- `money.code` must exist, be identical in both languages, and be exactly `DKK`
  (money never changes shape; a currency change edits the rule and this row together).
- `lang.switch_to` is the **action in the current UI language** ("Switch to Danish" /
  "Skift til engelsk") — the toggle's accessible name, not a language name.

## Coverage checker (the ratchet)

`uv run python scripts/i18n_check.py` is read-only and has five passes: **csv** (shape;
`scripts/_i18n.py` is delegated, never re-implemented), **reference** (`t()`/`tp()` ids
from the served code must exist), **dead-row** (every row is referenced by the app or a
test; a computed family needs a `DYNAMIC_FAMILIES` entry that has rows), **hardcoded**
(copy bypassing the CSV in `index.html`/`static/*.js`), and **ratchet** (which findings
are fatal). Exit `0` clean, `1` violations, `2` cannot run (bad args, invalid CSV).
`--quiet` prints only the summary, `--json` the findings; `scripts/run.sh` and CI refuse
a failing tree.

The ratchet is **global** now: `migrated.txt` (the namespace allowlist the migration
slices grew) is **deleted** — that deletion IS the flip, and it is why the file must
never come back. With no file, nothing is exempt: a hardcoded literal that matches any
CSV cell is an **error**, and the pinned INFO count is `0` (it may only ever shrink;
`tests/test_i18n_coverage.py` also asserts the file stays absent). During the migration
the file let unmigrated namespaces report INFO instead of failing; the price of that
transition is gone, and a new hardcoded string fails the gate immediately.

`copy-allowlist.txt` exempts findings by CONTENT — `<path>:<kind>:<escaped literal>`
(brand tokens, commands) — so an entry survives a line move and only goes stale when the
copy itself changes. It must say why and which item removes it, and the list can only
SHRINK: an unused entry is an error. Fix by class: unknown id → add the row (and `--write`) or fix the typo; dead row →
wire it or delete it; hardcoded copy → move it to the CSV and bind it; stale namespace or
allowlist entry → delete the entry when its last row/finding goes.

## Excel

Danish Excel uses `;` as its list separator, so a double-clicked CSV opens in one
column. Use **Data → From Text/CSV**, delimiter **comma**, encoding **UTF-8**, and keep
the BOM — it is what makes `æøå` survive.
