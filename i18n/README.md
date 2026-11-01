# i18n copy source

`strings.csv` is the **only** hand-edited source of user-visible copy. The app never
reads it: `scripts/build_i18n.py` compiles it into `static/i18n.messages.js`, which is
served and committed.

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

## Excel

Danish Excel uses `;` as its list separator, so a double-clicked CSV opens in one
column. Use **Data → From Text/CSV**, delimiter **comma**, encoding **UTF-8**, and keep
the BOM — it is what makes `æøå` survive.
