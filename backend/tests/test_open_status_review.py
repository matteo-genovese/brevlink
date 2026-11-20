"""Defects an independent review reproduced on 8a2689c, one test each.

Every test reasons about the real schema and the real HTTP surface. A test
whose point is the ordering between a read and a store runs both for real,
in two connections, on a database file; against PostgreSQL it runs only when
LIVE_DATABASE_URL names one.
"""
import uuid
from datetime import UTC, datetime, timedelta
import re

import pytest
from sqlalchemy import Text, cast, select

from sqlalchemy import event

from test_link_qr import _login
from test_link_qr import _create as _plain_create
from test_link_stats import _redirect
from test_open_status import _age_link, _ask, _db, _iso, _reliable, _seed, _status

MARKER_AT = datetime(2026, 11, 19, 10, 40, 40, 800000, tzinfo=UTC)
# Every scenario is anchored a day back, so no seed can land at or after the
# live "now" the endpoint stamps its bound with.
DAY_AGO = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
# A fixed past evening, for the cases where "a day later" must stay inside the
# endpoint's 7-day limit whatever the clock says when the test runs.
PAST = datetime(2026, 11, 1, 22, 0, 0, tzinfo=UTC)
SECONDS = datetime(2026, 11, 19, 10, 40, 50, tzinfo=UTC)


def _utc(value):
    from app.services.link_stats import _utc as parse
    return parse(value)


def _store_marker(client, slug, when):
    """The redirect's own marker write, at a stated instant."""
    from app.models.link import Link
    from app.services import link_stats

    async def write(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == slug))).scalar_one()
        await link_stats.record_click_uncertainty(db, link_id, now=when)
    _db(client, write)


def _put_marker(client, slug, first, last, add=False):
    """A marker row with these exact instants, as record_click_uncertainty keeps them.

    With `add`, a further marker in the same second: the row is kept and its
    last instant and count move, exactly as the writer's conflict branch does.
    """
    from app.models.link import Link
    from app.models.link_stats import LinkClickUncertainty

    # `hits` 2: markers are only ever written for a visit that then failed.
    values = {"link_id": None, "occurred_at": first.replace(microsecond=0), "first_at": first, "last_at": last, "hits": 2}

    async def write(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == slug))).scalar_one()
        if not add:
            # The one marker of this link, at exactly these instants.
            await db.execute(LinkClickUncertainty.__table__.delete().where(
                LinkClickUncertainty.link_id == link_id))
            # Core, not ORM: a freshly added row would show its column defaults,
            # not the instants written.
            await db.execute(LinkClickUncertainty.__table__.insert().values(**{**values, "link_id": link_id}))
        else:
            await db.execute(LinkClickUncertainty.__table__.update().where(
                LinkClickUncertainty.link_id == link_id).values(last_at=last, hits=LinkClickUncertainty.hits + 1))
        await db.flush()
    _db(client, write)


def _marker_rows(client, slug):
    from app.models.link import Link
    from app.models.link_stats import LinkClickUncertainty

    async def read(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == slug))).scalar_one()
        # The stored columns, not the ORM row: an unloaded attribute would
        # re-run its Python-side default instead of showing what was stored.
        rows = (await db.execute(select(
            LinkClickUncertainty.occurred_at, LinkClickUncertainty.first_at,
            LinkClickUncertainty.last_at, LinkClickUncertainty.hits,
        ).where(LinkClickUncertainty.link_id == link_id))).all()
        return [
            (_utc(bucket), _utc(first), _utc(last), hits)
            for bucket, first, last, hits in rows
        ]
    return _db(client, read)


def _links_by_id(client):
    from app.models.link import Link

    async def read(db):
        links = (await db.execute(select(Link))).scalars().all()
        return {link.slug: link.id for link in links}
    return _db(client, read)


def _snapshot(client):
    from app.models.link import Link
    from app.models.link_stats import LinkClickUncertainty

    async def read(db):
        links = (await db.execute(select(Link.id, Link.clicks, cast(Link.updated_at, Text)).order_by(Link.id))).all()
        markers = (await db.execute(select(LinkClickUncertainty))).scalars().all()
        return [tuple(row) for row in links], [(m.link_id, m.occurred_at, m.last_at, m.hits) for m in markers]
    return _db(client, read)


def _statements(client):
    """Every statement the next block runs on the app's engine."""
    from app.core.database import engine
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).lower())

    def start():
        seen.clear()
        event.listen(engine.sync_engine, "before_cursor_execute", record)

    return seen, start, lambda: event.remove(engine.sync_engine, "before_cursor_execute", record)


def _batch_lock(statements):
    """The one statement that takes the batch's row locks, as executed.

    It is the UPDATE setting each column to its own value, over the ids in id
    order; PostgreSQL renders its default row locks as `FOR NO KEY UPDATE` or
    `FOR SHARE` on the rows it visits, and the advisory lock that lets one
    batch in at a time (`pg_advisory_xact_lock`) precedes it there.
    """
    return [sql for sql in statements if sql.startswith("update links")]


def _advisory_locks(statements):
    return [sql for sql in statements if "pg_advisory_xact_lock" in sql]


def _link_writes(statements):
    """Statements that write a links row: the value-setting lock, never a real update."""
    return [sql for sql in statements if sql.startswith(("update links", "insert into links", "delete from links"))]


def test_a_marker_keeps_its_own_instant_apart_from_its_second_bucket(client):
    """:40.800 must stay :40.800 even where the conflict key is its second."""
    owner = _login(client)
    _reliable(client, owner)
    _age_link(client, "print-me", SECONDS - timedelta(days=1))
    # The sequence is fixed here, not read from the clock: before this change
    # every marker was stored at the whole second of "now".
    _put_marker(client, "print-me", MARKER_AT, MARKER_AT + timedelta(milliseconds=150))
    (bucket, first, last, hits), = _marker_rows(client, "print-me")
    # One bucket per second, as designed, and the precise instants in it.
    assert bucket == MARKER_AT.replace(microsecond=0)
    assert first == MARKER_AT
    assert last == MARKER_AT + timedelta(milliseconds=150)
    assert hits == 2
    # A third marker in the same second adds to that row, never collides.
    _put_marker(client, "print-me", MARKER_AT, MARKER_AT + timedelta(milliseconds=300), add=True)
    rows = _marker_rows(client, "print-me")
    # The bucket the three writes share is still there, unrounded, and grew.
    (bucket, first, last, hits), = [row for row in rows if row[0] == MARKER_AT.replace(microsecond=0)]
    assert first == MARKER_AT
    assert last == MARKER_AT + timedelta(milliseconds=300)
    assert hits == 3


async def _read_at(db, start, bound):
    """read_open_status with its one bound pinned: the instant after the locks."""
    from app.models.link import Link
    from app.services import link_stats

    caller = (await db.execute(select(Link.user_id).where(Link.slug == "print-me"))).scalar_one()
    return await link_stats.read_open_status(db, caller, "brevl.ink", [("print-me", start)], until=bound)


def test_a_marker_after_the_read_bound_cannot_hide_an_earlier_lost_click(client, monkeypatch):
    """Marker :40.800, request starts :40.500: that window is not withheld.

    The :40.500 visit could not be stored and its marker was written at
    :40.800. A request that started at :40.500 and fixed its bound at :40.700
    -- the instant right after its locks, 200 ms before that marker write --
    proves (start, :40.700], which the marker is later than. It answers over
    that window instead of calling it unknown for a marker it cannot have seen.
    With the marker stored at its whole second (:40.000) the same request
    withholds the answer, so the precise instant is what kept them apart.
    """
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent, LinkClickUncertainty
    from app.services import link_stats

    owner = _login(client)
    _reliable(client, owner)
    click = PAST + timedelta(seconds=1)            # :40.500 (whole seconds: SQLite)
    marker = click + timedelta(milliseconds=300)   # :40.800
    start, bound = click, marker - timedelta(milliseconds=100)   # :40.500, :40.700
    _age_link(client, "print-me", PAST - timedelta(days=1))

    async def seed(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == "print-me"))).scalar_one()
        db.add(LinkClickEvent(id=uuid.uuid4(), link_id=link_id, occurred_at=click, day=click.date(),
                              device="desktop", visitor_hash="c" * 32, hits=1))
        await db.flush()
    _db(client, seed)

    def read_at(bound):
        """The service read with its bound pinned, and every call it answered."""
        from app.models.link import Link as Model

        async def run(db):
            caller = (await db.execute(select(Model.user_id).where(Model.slug == "print-me"))).scalar_one()
            return await link_stats.read_open_status(db, caller, "brevl.ink", [("print-me", start)], until=bound)
        return _db(client, run)

    def put(last):
        """The link's one marker, at `last` (its bucket is that instant's second)."""
        async def replace(db):
            link_id = (await db.execute(select(Link.id).where(Link.slug == "print-me"))).scalar_one()
            await db.execute(LinkClickUncertainty.__table__.delete().where(LinkClickUncertainty.link_id == link_id))
            await db.execute(LinkClickUncertainty.__table__.insert().values(
                link_id=link_id, occurred_at=last.replace(microsecond=0),
                first_at=last, last_at=last, hits=2))
        _db(client, replace)

    # No marker yet: the window is the click, whatever the live clock does.
    checked, found = read_at(bound)
    assert _utc(checked) == bound
    assert found == {"print-me": click.replace(tzinfo=UTC)} or found == {"print-me": start.replace(tzinfo=UTC)}
    # The marker at :40.800, 100 ms after the bound: not in the window, so the
    # same answer. (Before this change every marker was kept at its second, and
    # this instance was read as :40.000 -- inside the window -- instead.)
    put(marker)
    assert read_at(bound)[1] == found
    # Stored at :40.000, inside the very same window: the lost visit it stands
    # for may be the first open there, so nothing is claimed for it.
    put(marker.replace(microsecond=0))
    assert read_at(bound)[1] == {}


def test_unserved_or_unreadable_status_acquires_no_lock_and_writes_nothing(client, monkeypatch):
    """Resolve first, authorize before locking: an unknown slug touches no row."""
    from app.api.v1 import links as links_api

    owner, other = _login(client), _login(client, "defect-other@example.com")
    _reliable(client, owner, slug="mine")
    _plain_create(client, other)  # someone else's, not opted in to open-status
    _age_link(client, "mine", DAY_AGO - timedelta(hours=1))
    since = _iso(DAY_AGO)
    before = _snapshot(client)
    seen, start, stop = _statements(client)
    for slug in ("missing", "print-me", "health", "privacy"):
        start()
        try:
            response = _status(client, owner, [{"slug": slug, "since": since}])
        finally:
            stop()
        assert response.status_code == 200 and response.json()["entries"] == [], slug
        assert _batch_lock(seen) == [] and _advisory_locks(seen) == [], (slug, seen)
    # Not one link row changed: no counter, no updated_at, no marker.
    assert _snapshot(client) == before
    # A readable entry is still locked, exactly once, before it is read.
    start()
    try:
        looked = _status(client, owner, [{"slug": "mine", "since": since}])
    finally:
        stop()
    assert looked.status_code == 200, looked.text
    assert looked.json()["entries"] == [{"slug": "mine", "opened_at": None}]
    # Exactly one batch lock, over the one row already checked: the UPDATE
    # that sets each column to its stored value and so takes the row without
    # changing it. (No advisory lock on SQLite, which has no row locks to order.)
    locks = _batch_lock(seen)
    assert len(locks) == 1, locks
    assert re.search(r"\bwhere links\.id in \(\?\)$", locks[0]), locks[0]
    assert "clicks=(links.clicks + ?)" in locks[0], locks[0]
    assert "updated_at=links.updated_at" in locks[0], locks[0]


def test_multiple_readable_links_are_locked_in_id_order_not_batch_order(client):
    """Two readers asking in opposite orders must not deadlock on each other."""
    owner = _login(client)
    slugs = ("alpha", "beta", "gamma")
    for slug in slugs:
        _reliable(client, owner, slug=slug)
        _age_link(client, slug, DAY_AGO - timedelta(hours=1))
    since = _iso(DAY_AGO)

    seen, start, stop = _statements(client)
    start()
    try:
        response = _status(client, owner, [{"slug": slug, "since": since} for slug in reversed(slugs)])
    finally:
        stop()
    # The response keeps the request's order, whatever order the locks took.
    assert [entry["slug"] for entry in response.json()["entries"]] == list(reversed(slugs))
    locks = _batch_lock(seen)
    # One statement for the whole batch, holding every link asked about (and
    # only those): the same set of rows in the same order, whatever the order
    # the batch named them and whichever reader asks first.
    assert len(locks) == 1, locks
    assert locks[0].endswith("where links.id in (" + ", ".join("?" for _ in slugs) + ")"), locks[0]
    # The order is the immutable id's, never the time or the batch's order:
    # that is what lets two batches take the rows the same way round.
    assert "order by" not in locks[0]
    assert len(set(_link_ids(client, slugs))) == len(slugs)


def test_every_entry_answers_over_one_bound_read_after_the_locks(client):
    """Three links, three markers and three clicks: one bound, read after the batch lock."""
    import app.services.links as link_service
    import app.services.link_stats as link_stats

    owner = _login(client)
    slugs = ("one", "two", "three")
    stored = []
    for slug in slugs:
        _reliable(client, owner, slug=slug)
        _age_link(client, slug, DAY_AGO - timedelta(days=1))
        _store_marker(client, slug, DAY_AGO - timedelta(hours=3))
        stored.append(_seed(client, [DAY_AGO - timedelta(hours=2)], slug=slug)[0])
    since = DAY_AGO - timedelta(minutes=1)

    order, original_lock, original_read = [], link_service.lock_links, link_stats._first_click_between

    async def observed_lock(db, ids):
        order.append(("lock", tuple(sorted(ids))))
        return await original_lock(db, ids)

    async def observed_read(db, link, start, until):
        order.append(("read", until))
        return await original_read(db, link, start, until)
    link_service.lock_links = observed_lock
    link_stats._first_click_between = observed_read
    try:
        checked, found = _db(client, lambda db: _read(db, slugs, since))
    finally:
        link_service.lock_links = original_lock
        link_stats._first_click_between = original_read

    # A marker dated before the window stands for a visit that may be inside
    # it, so these windows are unknown rather than zero: a slug with no answer
    # and no zero is left out, never claimed empty.
    assert found == {}, found
    assert checked > since
    # One lock over all three links, then each entry read over the one bound
    # the lock handed back: the window the response describes is the window
    # that was read.
    ids = _link_ids(client, slugs)
    assert order[0] == ("lock", tuple(sorted(ids))), (order[0], sorted(ids))
    reads = [until for kind, until in order[1:] if kind == "read"]
    assert len(reads) == len(slugs)
    assert set(reads) == {_utc(checked)}
    assert all(_utc(when) < _utc(checked) for when in stored)


def _link_ids(client, slugs):
    from app.models.link import Link

    async def read(db):
        return [link.id for link in (await db.execute(select(Link).where(Link.slug.in_(slugs)))).scalars()]
    return _db(client, read)


async def _read(db, slugs, since):
    """The caller's own read on this session: one user, the batch, one bound."""
    from app.models.link import Link
    from app.services.link_stats import read_open_status

    caller = (await db.execute(select(Link.user_id).where(Link.slug == slugs[0]))).scalar_one()
    return await read_open_status(db, caller, "brevl.ink", [(slug, since) for slug in slugs])




def test_a_repeat_rolled_into_an_earlier_row_is_not_answered_as_that_earlier_click(client):
    """first < since, hits 2: a repeat inside the window has no instant of its own.

    A visitor's first visit of the day is stored at 09:30. Their repeat at
    23:30 only raised that row's count. A window from 22:00 holds the 23:30
    visit, and the only instant recorded for it is 09:30 -- hours before the
    window. Answering that instant would report an open outside the window
    that was never asked about, and answer a repeat inside it as the first.
    """
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent

    owner = _login(client)
    _reliable(client, owner)
    _age_link(client, "print-me", DAY_AGO - timedelta(days=2))
    first = DAY_AGO.replace(hour=9, minute=30)

    async def seed(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == "print-me"))).scalar_one()
        db.add(LinkClickEvent(id=uuid.uuid4(), link_id=link_id, occurred_at=first, day=first.date(),
                              device="desktop", visitor_hash="v" * 32, hits=2))
        await db.flush()
    _db(client, seed)
    since = DAY_AGO.replace(hour=22, minute=0)
    # An instant before the window is no answer for it, and neither is zero.
    assert _ask(client, owner, "print-me", since) == {}
    # From before that day's first visit, the window holds the stored row and
    # whatever its count hides: the later open is not the first either.
    assert _ask(client, owner, "print-me", first - timedelta(hours=1)) == {}


def test_an_unresolved_marker_before_since_still_hides_a_later_positive(client):
    """A lost visit no later than its marker may be earlier than a proven click.

    A stored first visit at 09:00, a lost visit whose marker is at 10:40, and
    a click at 11:00. Read from 10:10 the 11:00 click would look like the
    first open, but the lost visit may be at 10:40, before it. The precise
    marker can be upper-bounded, but the visit cannot be placed from the
    window alone, so nothing is answered -- not a later possible first, and
    never a zero.
    """
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent

    owner = _login(client)
    _reliable(client, owner)
    now = DAY_AGO
    _age_link(client, "print-me", now - timedelta(days=1))
    start = now - timedelta(minutes=50)            # 10:10
    first = now - timedelta(hours=2)               # 09:00, stored, a single visit
    marker = start + timedelta(minutes=30)         # 10:40, the lost visit's marker
    click = now - timedelta(minutes=10)            # 11:00
    read_from = start                              # 10:10

    async def seed(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == "print-me"))).scalar_one()
        db.add(LinkClickEvent(id=uuid.uuid4(), link_id=link_id, occurred_at=first, day=first.date(),
                              device="desktop", visitor_hash="s" * 32, hits=1))
        await db.flush()
    _db(client, seed)
    _seed(client, [click], addresses=["203.0.113.77"])
    # No marker yet: the 11:00 click is the window's first open.
    assert _ask(client, owner, "print-me", read_from) == {"print-me": click.replace(tzinfo=UTC).isoformat()}
    # The lost visit's marker at 10:40, after the window starts and before the
    # click: it may be the earlier open, so the click is no longer an answer.
    _put_marker(client, "print-me", marker, marker)
    assert _ask(client, owner, "print-me", read_from) == {}
    # From just after everything: the lost visit could be before that start,
    # so the click is never offered as the first open. (A start after the
    # marker proves the window it covers; whether the lost visit came before
    # that start is not answerable from the rows, so it is no answer either.)
    assert _ask(client, owner, "print-me", click + timedelta(minutes=5)) == {}


def test_caddy_legal_paths_are_never_measured(client):
    """Caddy answers /privacy and its siblings from landing: the redirect never runs."""
    from app.services.link_stats import RESERVED_SLUGS

    assert {"privacy", "terms", "cookies", "legal", "subprocessors"} <= RESERVED_SLUGS
    owner = _login(client)
    slugs = ("privacy", "terms", "cookies", "legal", "subprocessors")
    for slug in slugs:
        _reliable(client, owner, slug=slug)
        _age_link(client, slug, DAY_AGO - timedelta(hours=1))
    since = _iso(DAY_AGO)
    response = _status(client, owner, [{"slug": slug, "since": since} for slug in [*slugs, "health"]])
    assert response.status_code == 200
    assert response.json()["entries"] == []
