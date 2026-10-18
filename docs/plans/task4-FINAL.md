# Task 4 FINAL spec — Server-enforced logoff deadline + qr-identify

Merged from `task4-proposal-{glm,kimi,qwen}.md` + reviews. Implementer follows THIS.

## Model (`app/models.py`)

- Add `logoff_deadline: datetime | None = Field(default=None, sa_type=DateTime)`
  to `AuthToken`.
- `app/main.py` `COLUMN_MIGRATIONS`: append
  `("auth_tokens", "logoff_deadline", "DATETIME", ())`. No index.

## `get_current_user` (`app/deps.py`)

After the existing missing/expired check, add: if
`auth_token.logoff_deadline is not None and utcnow() >= auth_token.logoff_deadline`
→ delete the token row (bounded, once; wrapped in try/except so a failed
cleanup still yields the 401) and raise 401 with the **uniform** existing
detail "Token invalid or expired". `expires_at` and `must_change_password`
handling unchanged.

## Grace constant (`app/security.py`)

`LOGOFF_GRACE_SECONDS = max(1, int(os.environ.get("HCRM_LOGOFF_GRACE_SECONDS", "30")))`.
Referenced via the module attribute at call time (so tests monkeypatch it to 0;
the env clamp does not interfere).

## Endpoints (`app/routers/auth.py`)

- `POST /api/auth/logoff-schedule` (authenticated, per-presented-token like
  logout) → set `logoff_deadline = min(utcnow() + LOGOFF_GRACE_SECONDS,
  token.expires_at)`, commit, return `{logoff_deadline, grace_seconds}`.
  Idempotent restart (same-badge re-scan restarts).
- `POST /api/auth/logoff-cancel` (authenticated, per-presented-token) → clear
  `logoff_deadline`, commit, 204. Idempotent.
- Neither endpoint is added to `MUST_CHANGE_EXEMPT_PATHS` (flagged accounts
  must change their password first; logout remains their escape hatch).

## `POST /api/auth/qr-identify`

- Authenticated (get_current_user → anonymous 401; flagged 403).
- Body reuses `QrLoginIn {token}` (badge never in a URL).
- Extract a shared `_badge_from_scanned(scanned) -> str | None` from `qr-login`
  (prefixed `HCRM1:` + raw 43-char token forms, one acceptance rule; existing
  qr tests are the regression gate).
- Response `{same_user: bool}`:
  - unparseable/empty badge → `{same_user: false}` (uniform, no enumeration);
  - else `same_user = hmac.compare_digest(token_hash(badge), user.qr_badge_hash)`
    (badgeless account → false).
- Zero side effects (no session minted, nothing armed).

## Tests

- `tests/test_logoff.py` (~15 cases, no sleeps — monkeypatch grace to 0 for
  scheduling; write a past deadline directly for expiry): gates; schedule →
  `{deadline, grace_seconds}` pin; deadline == now boundary (<=); restart
  overwrite; cancel idempotent; expiry 401 + row deleted (direct DB); uniform
  detail; per-token isolation (other sessions untouched); deadline clamps to
  `expires_at`; change-password/logout take armed rows with them; env override
  + clamp.
- `tests/test_qr.py` (+8 identify cases): both badge forms → true; other-user /
  garbage / badgeless → false; unparseable → false; no-side-effects DB row
  count; exact `{same_user}` shape; anonymous 401; flagged 403.
- `tests/test_migrations.py`: exact-set unchanged (the new column is listed);
  legacy-table upgrade case adds `logoff_deadline` via the migration.
