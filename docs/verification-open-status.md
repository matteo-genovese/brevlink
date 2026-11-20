# Verifying reliable open-status (Brevlink backend)

Scope: `POST /api/v1/links/open-status`, the `reliable_open_status` opt-in on
`POST /api/v1/links`, the redirect's 503 path, and the `link_click_uncertainty`
markers. Success is: the suite below passes, on SQLite and on PostgreSQL, and
`alembic upgrade head` applies on a fresh database.

## Prepare

```bash
cd backend
python -m venv <venv> && <venv>/bin/pip install -r requirements-dev.txt
```

## Exercise

```bash
# 26 open-status tests: the contract, the proof rules, the redirect outcomes,
# and the read/write fence (a store held open past a read).
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest tests/test_open_status.py tests/test_open_status_fence.py -q

# Full backend suite.
PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest tests -q

# Migration, on a throwaway database (same env as CI).
rm -f /tmp/brev-alembic-check.db
DATABASE_URL=sqlite+aiosqlite:////tmp/brev-alembic-check.db \
JWT_SECRET=12345678901234567890123456789012 DEBUG=false \
  <venv>/bin/alembic -c alembic.ini upgrade head
```

Expect `26 passed`, the full suite passing, and the last migration line
`0012_default_domain_slug -> 0013_reliable_open_status`.

The fence test also runs on PostgreSQL: give it a disposable server.

```bash
DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/brevtest \
LIVE_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/brevtest \
JWT_SECRET=12345678901234567890123456789012 DEBUG=false \
  <venv>/bin/python -m pytest tests/test_open_status.py tests/test_open_status_fence.py -q
```

It drops and rebuilds the schema in the named database: use a disposable one.

## What the open-status tests prove

- Opt-out links are never answered; opted-in links are.
- A repeat visit whose later click kept an earlier timestamp is never a zero.
- A gap marker in the interval withholds the answer, unless the first proven
  click is at or before the marker (then that click stands).
- Ordinary links still 307 when their click is lost; strict links 503 only when
  the click and the marker both fail, and never redirect in that case.
- Windows before the link's birth, on another host, on an unverified or
  suspended domain, on a paused or moderated (flagged) link, or on a link the
  caller cannot read are omitted.
- Slugs Caddy or FastAPI answer before the redirect (`/health`, `/docs`,
  `/redoc`, `/api`, `/app`, `openapi.json`, and the `/healthz`-style probes)
  are omitted: those paths never serve a link.
- One slug asked about two different starts in one batch is refused (422).
- A `since` that is not before the read's own bound is left out, and refused
  when it is the whole request: `(since, checked_through]` cannot be a zero.
- Every instant on the wire (`checked_through`, each positive) carries its UTC
  offset, including when SQLite hands it back naive.
- A click whose store is still open when a read runs is never answered as a
  zero: the click store and the read serialize on the link's own row.

## How the fence works

- A click store takes the link row's update lock first and commits last, and
  its click instant is read after that lock.
- The gap marker takes the same lock, then reads its instant.
- An open-status read takes the same lock for each entry, reads its instant
  after it, and commits after every entry: one waiting store is included, one
  that starts later records a later instant.
- On SQLite the app raises `busy_timeout` to 30 s per transaction so the read
  waits for the store instead of failing at `database is locked`.

## Pruning

`scripts/prune_click_events.py` deletes raw events older than
`CLICK_EVENT_RETENTION_DAYS`, and uncertainty markers older than the same
cutoff, by their own age. The oldest remaining event of any link never
decides whether another link's marker survives.

## Not covered by this procedure

`bandit` is not in the dev requirements, so the CI security step is not run
here.
