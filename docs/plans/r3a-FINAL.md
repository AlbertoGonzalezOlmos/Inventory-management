# Task A FINAL spec — backend: grace 60s, copy sweep, calendar aggregate

## 1. Logoff grace 30 → 60 (`app/security.py`)

`LOGOFF_GRACE_SECONDS = max(1, int(os.environ.get("HCRM_LOGOFF_GRACE_SECONDS", "60")))`
(default string "30" → "60"). Env override still wins.

## 2. Copy sweep (every "30 s"/"30 seconds" related to the logoff countdown)

Grep the tree for "30 s", "30s", "30 second", "thirty" tied to the logoff
countdown and update to 60: `static/index.html` (the "30 s" countdown copy and
any aria/comment), `static/app.js` comments, `README.md`, `docs/opticon-m10.md`,
`NOTES.md`, `app/security.py` docstrings, test docstrings/names where they hardcode
"30". Add/adjust the copy that states the countdown length.

## 3. `GET /api/orders/calendar` (new, authenticated member)

Query params `year` (int), `month` (1–12), both optional → default to the current
UTC month. Validate (422 on nonsense). Returns:

```json
{
  "year": 2026, "month": 10,
  "days": { "2026-10-01": {"cents": 1234, "count": 2}, "2026-10-03": {"cents": 99, "count": 1} },
  "total_cents": 1333
}
```

- Bucket by **UTC day** of each order's `created_at` (server-side `strftime('%Y-%m-%d', created_at)` grouping over `purchases` joined with `purchase_lines` summed `price_cents * applied_qty`), scoped to the requesting user.
- Only days with at least one purchase appear. `count` = number of orders that day.
- Reuse the staff-visible history pattern where relevant; this endpoint is member-self only (`get_current_user`).

## Tests (`tests/test_purchases.py` extension or new)

- default month == current UTC month; explicit year/month; 422 on invalid month (0, 13) / non-int.
- day bucketing: orders on the same UTC day sum; orders on different days appear under distinct keys.
- `count` = number of orders that day; `total_cents` = sum across the month.
- per-user isolation (a second user's orders are not included).
- empty month → `{}` days + `total_cents: 0`.
- `applied_qty` is what is summed (a clamped line contributes `price * applied`, not `price * qty`).

## Gates

`uv run pytest` green (backend-only change).
