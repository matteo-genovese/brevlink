# Verifying reliable open-status (Brevlink backend)

Scope: `POST /api/v1/links/open-status`, the `reliable_open_status` opt-in on
`POST /api/v1/links`, the redirect's 503 path, and the `link_click_uncertainty`
markers. Success is: the suite below passes and `alembic upgrade head` applies
on a fresh SQLite file.

## Prepare

```bash
cd backend
python -m venv <venv> && <venv>/bin/pip install -r requirements-dev.txt
```

## Exercise

```bash
# 18 open-status tests: the contract, the proof rules, both redirect outcomes.
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest tests/test_open_status.py -q

# Full backend suite (205 existing + the open-status tests).
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest tests -q

# Migration, on a throwaway database (same env as CI).
rm -f /tmp/brev-alembic-check.db
DATABASE_URL=sqlite+aiosqlite:////tmp/brev-alembic-check.db \
JWT_SECRET=12345678901234567890123456789012 DEBUG=false \
  <venv>/bin/alembic -c alembic.ini upgrade head
```

Expect `18 passed`, `212 passed`, and the last migration line
`0012_default_domain_slug -> 0013_reliable_open_status`.

## What the open-status tests prove

- Opt-out links are never answered; opted-in links are.
- A repeat visit whose later click kept an earlier timestamp is never a zero.
- A gap marker (a redirect whose click write failed) makes every covering
  window unknown, never zero.
- Ordinary links still 307 when their click is lost; strict links 503 only when
  the click and the marker both fail, and never redirect in that case.
- Windows before the link's birth, on another host, on an unverified or
  suspended domain, or on a link the caller cannot read are omitted.
- One slug asked about two different starts in one batch is refused (422).

## Not covered by this procedure

No PostgreSQL server exists in the local environment and Docker/system
installs are out of scope, so the PostgreSQL path is compiled-only: the
migration's column/table DDL and the marker's `GREATEST` update were rendered
with the `postgresql` dialect, not executed. Run the same commands with a
`postgresql+asyncpg://` `DATABASE_URL` against a disposable server to cover it.
