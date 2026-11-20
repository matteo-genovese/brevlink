"""Batched open-status answers: positive evidence only, unreadable is omitted."""

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import pytest
from sqlalchemy import select


from test_link_qr import _create, _domain, _login
from test_link_stats import _db, _redirect


def _status(client, headers, entries, **body):
    # The default host, as the marketplace client reaches it: without it the
    # answer would cover no host at all, and own links on the default domain
    # are not served there.
    return client.post(
        "/api/v1/links/open-status", headers={**HOST, **headers},
        json={"entries": entries, **body},
    )


# The host the links live on in these tests.
HOST = {"host": "brevl.ink"}


def _iso(when):
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _reliable(client, headers, **extra):
    """A link that opted in to open-status answers (the default is off)."""
    return _create(client, headers, **{"reliable_open_status": True, **extra})


def _seed(client, whens, slug="print-me", addresses=None):
    """Record clicks; return each row's stored instant.

    Each visit is stamped at `when` (a backfilled event is that old) and gets
    the address in `addresses`, by default a distinct one per visit: identity
    comes from the address, so only same-address visits share a row. A click
    can only exist after its link did, so the link is aged before the first.
    """
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent
    from app.services.link_stats import record_click
    from starlette.requests import Request

    addresses = addresses or [f"203.0.113.{index + 1}" for index in range(len(whens))]

    async def seed(db):
        link = (await db.execute(select(Link).where(Link.slug == slug))).scalar_one()
        for address, when in zip(addresses, whens):
            request = Request({"type": "http", "headers": [(b"user-agent", b"Mozilla Linux")], "client": (address, 9)})
            await record_click(db, link, request, now=when)
        return sorted(event.occurred_at for event in (await db.execute(select(LinkClickEvent))).scalars())
    _age_link(client, slug, min(whens) - timedelta(days=2))
    return _db(client, seed)


def _age_link(client, slug, when):
    """Born at `when`: a seeded click has to exist after its link did."""
    from app.models.link import Link

    when = (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(UTC)

    async def age(db):
        # Never later than before: a later redirect could then be newer than
        # the click it explains. _db commits the result, so hand back no cursor.
        await db.execute(
            Link.__table__.update()
            .where(Link.slug == slug, Link.__table__.c.created_at > when)
            .values(created_at=when)
        )

    _db(client, age)


def _answers(payload):
    return {entry["slug"]: entry["opened_at"] for entry in payload["entries"]}


def _born(client, slug):
    """The instant the link was actually created, as stored (UTC)."""
    from app.models.link import Link

    async def read(db):
        from sqlalchemy import cast, Text
        return (await db.execute(select(cast(Link.__table__.c.created_at, Text)).where(Link.slug == slug))).scalar()
    return _utc(_db(client, read))


def _ask(client, headers, slug, since, **body):
    # Naive instants in these tests are UTC. The start is asked exactly as
    # given: moving it would answer a different question.
    since = (since if since.tzinfo else since.replace(tzinfo=UTC)).astimezone(UTC)
    return _answers(_status(client, headers, [{"slug": slug, "since": _iso(since)}], **body).json())


def _last_click(client, slug):
    """The instant of the newest stored click of `slug`, as stored (UTC)."""
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent
    from sqlalchemy import cast, Text

    async def read(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == slug))).scalar_one()
        return (await db.execute(
            select(cast(LinkClickEvent.occurred_at, Text))
            .where(LinkClickEvent.link_id == link_id)
            .order_by(LinkClickEvent.occurred_at.desc()).limit(1)
        )).scalar()
    return _utc(_db(client, read))


def _utc(stamp):
    from app.services.link_stats import _utc as parse
    return parse(stamp)


def test_contract_shape_and_zero_vs_positive(client):
    owner = _login(client)
    _reliable(client, owner)
    _reliable(client, owner, slug="quiet")
    now = datetime.now(UTC)
    (stored,) = _seed(client, [now - timedelta(hours=5)])
    # Stored instants come back naive from SQLite, so compare in naive UTC.
    since = now.replace(tzinfo=None) - timedelta(hours=6)

    response = _status(client, owner, [{"slug": "print-me", "since": _iso(since)}, {"slug": "quiet", "since": _iso(since)}])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"checked_through", "entries"}
    checked = datetime.fromisoformat(payload["checked_through"])
    assert checked.tzinfo is not None and abs(checked - now) < timedelta(minutes=1)
    assert all(set(entry) == {"slug", "opened_at"} for entry in payload["entries"])
    answers = _answers(payload)
    assert set(answers) == {"print-me"}
    opened = datetime.fromisoformat(answers["print-me"]).replace(tzinfo=None)
    assert since < opened <= checked.replace(tzinfo=None)
    assert opened == stored


def test_repeat_visit_hidden_behind_an_earlier_row_is_never_zero(client):
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent

    owner = _login(client)
    _reliable(client, owner)
    # A fixed past day, inside the 7-day limit: the local clock may still be
    # before 09:00 of today, which would put that hour in the future.
    day = (datetime.now(UTC) - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    _age_link(client, "print-me", day - timedelta(days=1))
    # 09:00, this visitor's first visit of the day. Their repeat at 10:30 the
    # same day only raised this row's count: no row is dated 10:30.
    first = day.replace(hour=9)

    async def seed(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == "print-me"))).scalar_one()
        db.add(LinkClickEvent(id=uuid.uuid4(), link_id=link_id, occurred_at=first, day=first.date(),
                              device="desktop", visitor_hash="r" * 32, hits=2))
        await db.flush()
    _db(client, seed)

    # The interval that includes the known first visit can report it. The
    # later interval cannot timestamp the repeat or report a zero.
    result = _ask(client, owner, "print-me", first - timedelta(minutes=5))
    assert _utc(result["print-me"]) == first.replace(tzinfo=UTC)
    assert _ask(client, owner, "print-me", first + timedelta(minutes=45)) == {}


def test_a_later_visit_on_an_earlier_day_is_not_hidden(client):
    """A click keeps its day's first timestamp; a later one that day is only in the total."""
    owner = _login(client)
    _reliable(client, owner)
    now = datetime.now(UTC).replace(tzinfo=None)
    first = now - timedelta(days=1)
    _seed(client, [first, first + timedelta(hours=1)], addresses=["203.0.113.9", "203.0.113.9"])
    # The stored row says yesterday 10:00: "not opened since yesterday 11:00"
    # would hide the 11:00 visit, and the clicked total says one is missing.
    assert _ask(client, owner, "print-me", first + timedelta(minutes=30)) == {}
    assert _ask(client, owner, "print-me", now - timedelta(hours=1)) == {}


def test_unidentifiable_visits_keep_their_own_rows(client, monkeypatch):
    """Without an identity every visit is its own row, so totals still agree."""
    owner = _login(client)
    _reliable(client, owner)
    now = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr("app.services.link_stats._visitor_hash", lambda request, day: None)
    _seed(client, [now - timedelta(hours=2), now - timedelta(hours=1)])
    # Both visits are inside the window: the first one is the answer.
    answered = _ask(client, owner, "print-me", now - timedelta(hours=3))
    assert datetime.fromisoformat(answered["print-me"]).replace(tzinfo=None) == (now - timedelta(hours=2)).replace(tzinfo=None)


def test_duplicate_slug_entries_need_identical_intervals(client):
    owner = _login(client)
    _reliable(client, owner)
    now = datetime.now(UTC).replace(tzinfo=None)
    _seed(client, [now - timedelta(hours=3)])
    narrow, wide = now - timedelta(minutes=30), now - timedelta(hours=4)

    # One slug gets one answer, read from whichever entry came first: two
    # different intervals have no order-independent answer, so they are
    # refused instead of answered from an arbitrarily chosen interval.
    for order in ([narrow, wide], [wide, narrow]):
        assert _status(client, owner, [{"slug": "print-me", "since": _iso(value)} for value in order]).status_code == 422
    # The same interval repeated (also via the batch default) is one question.
    repeated = [{"slug": "print-me", "since": _iso(wide)}] * 2
    for payload in (repeated, [{"slug": "print-me"}, {"slug": "print-me"}], [{"slug": "print-me", "since": _iso(wide)}]):
        body = {} if payload is repeated else {"since": _iso(wide)}
        response = _status(client, owner, payload, **body)
        assert response.status_code == 200, response.text
        assert len(response.json()["entries"]) == 1


def test_invalid_batches_are_rejected(client):
    owner = _login(client)
    _reliable(client, owner)
    now = datetime.now(UTC)
    valid = {"slug": "print-me", "since": _iso(now - timedelta(hours=1))}
    for entries in (
        [],
        [dict(valid, since=now.replace(tzinfo=None).isoformat())],  # no timezone
        [dict(valid, since=_iso(now + timedelta(hours=1)))],  # future
        [dict(valid, since="2020-01-01T00:00:00Z")],  # older than allowed
        [dict(valid, slug="bad slug!")],
        [{"slug": "print-me"}],  # no since anywhere
        [valid] * 501,
    ):
        assert _status(client, owner, entries).status_code == 422
    for body in ({"entries": "x"}, {}):
        assert client.post("/api/v1/links/open-status", headers=owner, json=body).status_code == 422
    assert _status(client, owner, [valid] * 100).status_code == 200
    # A client reporting its whole 7-day window can send slightly more than the
    # 100 it aims for: bounded, and answered (see OpenStatusRequest).
    assert _status(client, owner, [valid] * 120).status_code == 200
    assert _status(client, owner, [valid] * 500).status_code == 200
    # The client's own payload carries both keys; per-entry wins over batch.
    assert _status(client, owner, [valid], since=_iso(now)).status_code == 200
    assert _status(client, owner, [{"slug": "quiet"}], since=_iso(now - timedelta(hours=1))).status_code == 200


def test_batch_with_one_shared_since_is_read_per_slug(client):
    """The client sends one `since` for the batch, entries may omit their own."""
    owner = _login(client)
    link = _reliable(client, owner)
    # A link just created answers inside its own age; record a real visit, as
    # a marketplace open would, and ask from its birth.
    since = _iso(_born(client, link["slug"]))
    assert _redirect(client).status_code == 307
    response = client.post(
        "/api/v1/links/open-status", headers=owner,
        json={"host": "brevl.ink", "since": since,
              "entries": [{"slug": link["slug"]}]},  # no per-entry since
    )
    assert response.status_code == 200, response.text
    assert [entry["slug"] for entry in response.json()["entries"]] == [link["slug"]]
    assert response.json()["entries"][0]["opened_at"] is not None


def test_interval_is_bounded_by_seven_days_and_retention(client, monkeypatch):
    from app.core.config import settings

    owner = _login(client)
    _reliable(client, owner)
    now = datetime.now(UTC)
    entry = {"slug": "print-me", "since": _iso(now - timedelta(days=6, hours=23))}
    assert _status(client, owner, [entry]).status_code == 200
    assert _status(client, owner, [dict(entry, since=_iso(now - timedelta(days=7, minutes=1)))]).status_code == 422
    # A retention shorter than 7 days bounds the interval too: earlier events may be pruned.
    monkeypatch.setattr(settings, "click_event_retention_days", 3)
    assert _status(client, owner, [dict(entry, since=_iso(now - timedelta(days=4)))]).status_code == 422
    assert _status(client, owner, [dict(entry, since=_iso(now - timedelta(days=2)))]).status_code == 200


def test_only_opted_in_links_are_answered(client):
    owner = _login(client)
    _create(client, owner)  # the default: reliable_open_status is off
    now = datetime.now(UTC)
    _seed(client, [now - timedelta(hours=1)])
    since = _iso(now - timedelta(minutes=30))
    # Its click may be lost without a trace, so a zero would not be measured.
    assert _answers(_status(client, owner, [{"slug": "print-me", "since": since}]).json()) == {}

    # The opt-in is part of the create contract and of the link itself.
    opted = _reliable(client, owner, slug="opted")
    assert opted["reliable_open_status"] is True
    links = {item["slug"]: item for item in client.get("/api/v1/links", headers=owner).json()["items"]}
    assert links["print-me"]["reliable_open_status"] is False
    assert links["opted"]["reliable_open_status"] is True
    assert _redirect(client, "opted").status_code == 307
    assert _ask(client, owner, "opted", _born(client, "opted"))["opted"] is not None


def test_earlier_day_cannot_hide_a_click_in_the_interval(client):
    owner = _login(client)
    _reliable(client, owner)
    now = datetime.now(UTC).replace(tzinfo=None)
    _seed(client, [now - timedelta(days=1)])
    assert _ask(client, owner, "print-me", now - timedelta(hours=1)) == {"print-me": None}


def test_filtered_bot_and_prefetch_traffic_reads_as_zero(client):
    owner = _login(client)
    _reliable(client, owner)
    _age_link(client, "print-me", datetime.now(UTC) - timedelta(days=1))
    for headers in ({"user-agent": "TelegramBot (like TwitterBot)"}, {"user-agent": "Slackbot-LinkExpanding"}, {"purpose": "prefetch"}):
        assert _redirect(client, **headers).status_code == 307
    # None of those was an accepted click, so the period is a measured zero.
    assert _ask(client, owner, "print-me", datetime.now(UTC) - timedelta(minutes=5)) == {"print-me": None}
    assert _redirect(client).status_code == 307
    # A window from just before that click reports it, with its exact instant.
    click = _last_click(client, "print-me")
    reported = _ask(client, owner, "print-me", click - timedelta(milliseconds=5))
    assert datetime.fromisoformat(reported["print-me"]).replace(tzinfo=None) == click.replace(tzinfo=None)


def test_missing_foreign_and_inactive_links_are_omitted(client, monkeypatch):
    from app.services import domain_sharing

    monkeypatch.setattr(domain_sharing, "mailer", client.test_mailer)
    owner = _login(client)
    link = _reliable(client, owner)
    other = _login(client, "open-status-other@example.com")
    domain_id = _domain(client, owner, monkeypatch)
    _reliable(client, owner, slug="shared", domain_id=domain_id)
    # Both were never opened and both are owned here.
    _age_link(client, "print-me", datetime.now(UTC) - timedelta(hours=2))
    _age_link(client, "shared", datetime.now(UTC) - timedelta(hours=2))
    since = _iso(datetime.now(UTC) - timedelta(hours=1, minutes=30))

    def ask(headers, **body):
        slugs = ("print-me", "inactive", "missing", "shared", "print-me")
        response = _status(client, headers, [{"slug": slug, "since": since} for slug in slugs], **body)
        assert response.status_code == 200
        return [entry["slug"] for entry in response.json()["entries"]]

    custom = {"host": "go.example.com"}
    # A duplicate entry is answered once; an unowned slug is omitted. Read
    # order follows the batch. Each link is asked on the host it is served on.
    assert ask(owner) == ["print-me"]
    assert ask(owner, **custom) == ["shared"]
    assert client.patch(f"/api/v1/links/{link['id']}", headers=owner, json={"is_active": False}).status_code == 200
    # Paused: the redirect 404s it now, so its window measured no open and no
    # zero may say it did (see test_paused_and_flagged_links_are_omitted).
    assert ask(owner) == []
    assert ask(other, **custom) == []

    # Domain membership does not grant access to another owner's opens.
    assert client.post(f"/api/v1/domains/{domain_id}/members", headers=owner, json={"email": "open-status-other@example.com"}).status_code == 201
    token = client.test_mailer.messages[-1].text.split("token=")[1].split()[0]
    assert client.post("/api/v1/domains/invites/accept", headers=other, json={"token": token}).status_code == 200
    assert ask(other, **custom) == []
    assert ask(other) == []


def _fail(*args, **kwargs):
    raise RuntimeError("analytics unavailable")


def _markers(client):
    from app.models.link_stats import LinkClickUncertainty

    async def read(db):
        return (await db.execute(select(LinkClickUncertainty))).scalars().all()
    return _db(client, read)


def test_ordinary_link_still_redirects_when_its_click_is_lost(client, monkeypatch):
    """Not opted in: today's behaviour, and no promise is made about it."""
    owner = _login(client)
    _create(client, owner)
    since = _iso(datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr("app.api.v1.redirect.record_click", _fail)
    response = _redirect(client)
    assert response.status_code == 307
    assert response.headers["location"] == "https://example.com/destination"
    assert len(_markers(client)) == 1  # the gap is still recorded, for the record
    # Nothing is answered for it, so no zero can be wrong.
    assert _answers(_status(client, owner, [{"slug": "print-me", "since": since}]).json()) == {}


def test_strict_link_stops_when_both_writes_fail(client, monkeypatch):
    """No marketplace open happened, so a later zero is not false."""
    owner = _login(client)
    _reliable(client, owner)
    monkeypatch.setattr("app.api.v1.redirect.record_click", _fail)
    monkeypatch.setattr("app.api.v1.redirect.record_click_uncertainty", _fail)
    response = _redirect(client)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert "location" not in response.headers
    page = client.get("/print-me", headers={"host": "brevl.ink", "accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 503 and "try again" in page.text
    assert _markers(client) == []


def test_strict_link_redirects_once_the_gap_is_recorded(client, monkeypatch):
    """The user is not held back when the gap itself is provable."""
    owner = _login(client)
    _reliable(client, owner)
    monkeypatch.setattr("app.api.v1.redirect.record_click", _fail)
    assert _redirect(client).status_code == 307
    since = _iso(datetime.now(UTC) - timedelta(minutes=5))
    # An accepted click may have happened in that window: unknown, not zero.
    assert _answers(_status(client, owner, [{"slug": "print-me", "since": since}]).json()) == {}
    # And it stays unknown for the window from just before the marker on.
    assert _answers(_status(client, owner, [{"slug": "print-me", "since": _iso(datetime.now(UTC) - timedelta(seconds=5))}]).json()) == {}


def test_unverified_or_suspended_domain_is_omitted(client, monkeypatch):
    import uuid

    from app.models.domain import Domain

    owner = _login(client)
    domain_id = _domain(client, owner, monkeypatch)
    _reliable(client, owner, slug="on-custom", domain_id=domain_id)
    _age_link(client, "on-custom", datetime.now(UTC) - timedelta(hours=2))
    since = _iso(datetime.now(UTC) - timedelta(hours=1))

    def ask():
        response = _status(client, owner, [{"slug": "on-custom", "since": since}], host="go.example.com")
        assert response.status_code == 200, response.text
        return [entry["slug"] for entry in response.json()["entries"]]

    assert ask() == ["on-custom"]
    for flags in ({"is_suspended": True}, {"is_verified": False}):
        _db(client, lambda db, flags=flags: db.execute(
            Domain.__table__.update().where(Domain.id == uuid.UUID(domain_id)).values(**flags)))
        # The redirect 404s there, so a zero would promise what it denies.
        assert ask() == []
    _db(client, lambda db: db.execute(
        Domain.__table__.update().where(Domain.id == uuid.UUID(domain_id)).values(is_verified=True, is_suspended=False)))
    assert ask() == ["on-custom"]


def test_click_at_the_bound_is_reported(client):
    """A click exactly at `since` is in the interval: (since, through] includes it."""
    owner = _login(client)
    _reliable(client, owner)
    _seed(client, [datetime.now(UTC) - timedelta(hours=2)])
    since = _last_click(client, "print-me")
    reported = _ask(client, owner, "print-me", since)
    assert datetime.fromisoformat(reported["print-me"]).replace(tzinfo=None) == since.replace(tzinfo=None)


def test_auth_creation_and_redirect_stay_unchanged(client):
    batch = {"entries": [{"slug": "print-me", "since": _iso(datetime.now(UTC))}]}
    assert client.post("/api/v1/links/open-status", json=batch).status_code in (401, 403)
    owner = _login(client)
    link = _create(client, owner)
    assert link["short_url"] == "https://brevl.ink/print-me"
    response = _redirect(client)
    assert response.status_code == 307
    assert response.headers["location"] == "https://example.com/destination"
    assert client.get("/api/v1/links/print-me", headers=owner).json()["slug"] == "print-me"
    assert client.get("/api/v1/links/missing", headers=owner).status_code == 404


def test_answers_and_bound_carry_their_utc_offset(client):
    """SQLite hands stored instants back naive; the answer names the offset."""
    owner = _login(client)
    link = _reliable(client, owner)
    # A positive: its own instant, with UTC on the wire.
    _seed(client, [datetime.now(UTC) - timedelta(hours=2)], slug=link["slug"])
    since = _iso(datetime.now(UTC) - timedelta(hours=3))
    response = _status(client, owner, [{"slug": link["slug"], "since": since}])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert datetime.fromisoformat(payload["checked_through"]).utcoffset() == timedelta(0)
    assert datetime.fromisoformat(payload["entries"][0]["opened_at"]).utcoffset() == timedelta(0)


def test_a_since_beyond_checked_through_is_the_same_question(client):
    """Every entry is asked over the same interval: measured from its start, or refused.

    An entry whose start is not before checked_through has an empty interval;
    with one shared start for the batch that cannot be told apart from the
    others, so it is answered exactly like them.
    """
    owner = _login(client)
    _reliable(client, owner, slug="quiet")
    _reliable(client, owner, slug="other")
    for slug in ("quiet", "other"):
        _age_link(client, slug, datetime.now(UTC) - timedelta(days=1))
    # Future start (inside the +1 min clock skew): refused before any read.
    assert _status(client, owner, [{"slug": "quiet", "since": _iso(datetime.now(UTC) + timedelta(seconds=30))}]).status_code == 422
    # A real interval, both links: both are measured zeroes.
    past = _status(client, owner, [{"slug": "quiet", "since": _iso(datetime.now(UTC) - timedelta(minutes=5))},
                                   {"slug": "other", "since": _iso(datetime.now(UTC) - timedelta(minutes=5))}])
    assert _answers(past.json()) == {"quiet": None, "other": None}


def test_exact_start_counts_a_click_at_that_instant(client):
    owner = _login(client)
    link = _reliable(client, owner)
    _seed(client, [datetime.now(UTC) - timedelta(hours=2)], slug=link["slug"])
    since = _last_click(client, link["slug"])
    assert _utc(_ask(client, owner, link["slug"], since)[link["slug"]]) == since


def test_reserved_paths_are_not_measured(client):
    """Caddy and the app answer these paths before the redirect: no clicks to measure."""
    from app.services.link_stats import RESERVED_SLUGS

    assert {"health", "docs", "redoc", "api", "app"} <= RESERVED_SLUGS
    owner = _login(client)
    _reliable(client, owner, slug="health")
    _reliable(client, owner, slug="Privacy")
    _age_link(client, "health", datetime.now(UTC) - timedelta(hours=1))
    _age_link(client, "Privacy", datetime.now(UTC) - timedelta(hours=1))
    since = _iso(datetime.now(UTC) - timedelta(minutes=1))
    # ("openapi.json" is not a legal slug anyway: the schema refuses the dot.)
    asked = [slug for slug in sorted(RESERVED_SLUGS) if "." not in slug] + ["Privacy"]
    response = _status(client, owner, [{"slug": slug, "since": since} for slug in asked])
    assert response.status_code == 200, response.text
    assert response.json()["entries"] == []


def test_repeat_inside_window_preserves_the_first_open(client):
    owner = _login(client)
    _reliable(client, owner)
    first = datetime.now(UTC).replace(hour=10, minute=0, second=0) - timedelta(days=1)
    _seed(client, [first, first + timedelta(minutes=1)], addresses=["203.0.113.1"] * 2)
    assert _utc(_ask(client, owner, "print-me", first - timedelta(minutes=1))["print-me"]) == first


def test_link_paused_between_initial_lookup_and_lock_is_omitted(client, monkeypatch):
    from app.models.link import Link
    from app.services import links

    owner = _login(client)
    _reliable(client, owner)
    _age_link(client, "print-me", datetime.now(UTC) - timedelta(hours=1))
    lock_links = links.lock_links

    async def pause_before_lock(db, ids):
        await db.execute(Link.__table__.update().where(Link.id.in_(ids)).values(is_active=False))
        await lock_links(db, ids)

    monkeypatch.setattr(links, "lock_links", pause_before_lock)
    assert _ask(client, owner, "print-me", datetime.now(UTC) - timedelta(minutes=1)) == {}


def test_paused_and_flagged_links_are_omitted(client):
    """The redirect 404s a paused or flagged link: a zero there measures nothing."""
    from app.models.link import Link

    owner = _login(client)
    paused = _reliable(client, owner, slug="paused")
    flagged = _reliable(client, owner, slug="flagged")
    _reliable(client, owner, slug="never-opened")
    for slug in ("paused", "flagged", "never-opened"):
        _age_link(client, slug, datetime.now(UTC) - timedelta(hours=1))
    since = _iso(datetime.now(UTC) - timedelta(minutes=1))
    assert client.patch(f"/api/v1/links/{paused['id']}", headers=owner, json={"is_active": False}).status_code == 200
    _db(client, lambda db: db.execute(Link.__table__.update().where(Link.slug == "flagged").values(is_flagged=True)))
    for slug in ("paused", "flagged"):
        assert client.get(f"/{slug}", headers={"host": "brevl.ink", "accept": "application/json"}).status_code == 404
    assert _answers(_status(client, owner, [{"slug": slug, "since": since}
                                            for slug in ("paused", "flagged", "never-opened")]).json()) == {"never-opened": None}
    # Un-paused, the link is served again and its window is measured again.
    assert client.patch(f"/api/v1/links/{paused['id']}", headers=owner, json={"is_active": True}).status_code == 200
    assert _ask(client, owner, "paused", datetime.now(UTC) - timedelta(seconds=30)) == {"paused": None}


def _mark(client, slug, when):
    from app.models.link import Link
    from app.models.link_stats import LinkClickUncertainty

    async def write(db):
        link_id = (await db.execute(select(Link.id).where(Link.slug == slug))).scalar_one()
        db.add(LinkClickUncertainty(link_id=link_id, occurred_at=when, first_at=when, last_at=when, hits=1))
        await db.flush()
    _db(client, write)


def test_uncertainty_interval(client):
    """A marker seen after the first click: the click is still the first open."""
    owner = _login(client)
    link = _reliable(client, owner)
    now = datetime.now(UTC).replace(microsecond=0)
    first = now - timedelta(hours=3)
    _seed(client, [first], slug=link["slug"])
    _mark(client, link["slug"], now.replace(microsecond=0) - timedelta(hours=5))
    assert _ask(client, owner, link["slug"], now - timedelta(hours=4)) == {link["slug"]: first.isoformat()}
    # (The marker is epoch, from the suite's own seed of another link; see
    # test_marker_inside_the_interval_withholds_the_first_open for the in-window case.)


def test_marker_inside_the_interval_withholds_the_first_open(client):
    """The lost click a marker stands for may precede the proven one: answer nothing."""
    owner = _login(client)
    link = _reliable(client, owner)
    now = datetime.now(UTC).replace(microsecond=0)
    first = now - timedelta(hours=3)
    _seed(client, [first], slug=link["slug"])
    _mark(client, link["slug"], now.replace(microsecond=0) - timedelta(hours=3, seconds=1))
    # The window that contains the marker: no later first open.
    assert _ask(client, owner, link["slug"], now - timedelta(hours=3, minutes=30)) == {}
    # A window starting after the marker: the click alone is in it, and it is the answer.
    assert _ask(client, owner, link["slug"], first - timedelta(microseconds=500000)) == {link["slug"]: first.isoformat()}
