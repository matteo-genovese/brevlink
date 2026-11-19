"""Batched open-status answers: positive evidence only, unreadable is omitted."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from test_link_qr import _create, _domain, _login
from test_link_stats import _db, _redirect


def _status(client, headers, entries, **body):
    return client.post("/api/v1/links/open-status", headers=headers, json={"entries": entries, **body})


def _iso(when):
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seed(client, whens, slug="print-me", now=None):
    """Record clicks from distinct visitors; return each row's stored instant."""
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent
    from app.services.link_stats import record_click
    from starlette.requests import Request

    # Each visit is stamped at `when` (a backfilled event is that old).
    def visitor(index):
        return Request({"type": "http", "headers": [(b"user-agent", b"Mozilla Linux")], "client": (f"203.0.113.{index + 1}", 9)})

    async def seed(db):
        link = (await db.execute(select(Link).where(Link.slug == slug))).scalar_one()
        for index, when in enumerate(whens):
            await record_click(db, link, visitor(index), now=when)
        return sorted(event.occurred_at for event in (await db.execute(select(LinkClickEvent))).scalars())
    return _db(client, seed)


def _answers(payload):
    return {entry["slug"]: entry["opened_at"] for entry in payload["entries"]}


def _ask(client, headers, slug, since):
    return _answers(_status(client, headers, [{"slug": slug, "since": _iso(since)}]).json())


@pytest.fixture()
def ordinary_rows(monkeypatch):
    """Keep separate rows for separate visitors, as real fingerprints would.

    The test client reuses one peer address, and without a configured secret
    no fingerprint is calculable, so every click would collapse into one row.
    """
    monkeypatch.setattr("app.services.link_stats._visitor_hash", lambda request, day: f"{id(request):032d}")


def test_contract_shape_and_zero_vs_positive(client):
    owner = _login(client)
    _create(client, owner)
    _create(client, owner, slug="quiet")
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
    assert answers["quiet"] is None
    opened = datetime.fromisoformat(answers["print-me"]).replace(tzinfo=None)
    assert since < opened <= checked.replace(tzinfo=None)
    assert opened == stored


def test_repeat_visit_is_never_answered_as_zero(client, ordinary_rows):
    owner = _login(client)
    _create(client, owner)
    now = datetime.now(UTC).replace(tzinfo=None)
    first = now - timedelta(hours=2)
    _seed(client, [first, first + timedelta(seconds=1)])  # one visitor twice: hits -> 2

    # The later visit has no timestamp of its own. Neither an interval that
    # contains it nor a later one can claim no click happened.
    assert _ask(client, owner, "print-me", now - timedelta(hours=1, minutes=30)) == {}
    assert _ask(client, owner, "print-me", now - timedelta(minutes=30)) == {}


def test_unidentifiable_rows_make_absence_unprovable(client, monkeypatch):
    """Clicks recorded without a fingerprint still show the link was opened."""
    owner = _login(client)
    _create(client, owner)
    now = datetime.now(UTC)
    _seed(client, [now - timedelta(minutes=30)])
    monkeypatch.setattr("app.services.link_stats._visitor_hash", lambda request, day: None)
    assert _ask(client, owner, "print-me", now - timedelta(minutes=5)) == {}


def test_duplicate_slug_entries_keep_their_own_interval(client):
    owner = _login(client)
    _create(client, owner)
    now = datetime.now(UTC).replace(tzinfo=None)
    _seed(client, [now - timedelta(hours=3)])
    narrow, wide = now - timedelta(minutes=30), now - timedelta(hours=4)

    # Two entries for one slug are two questions: one before the click
    # reports it, a later one cannot claim "no click". Repeating the request
    # keeps the same answers, whatever order the entries arrive in.
    opened = datetime.fromisoformat(_ask(client, owner, "print-me", wide)["print-me"]).replace(tzinfo=None)
    assert opened < now - timedelta(hours=2)
    for order in ([narrow], [narrow, wide], [wide, narrow]):
        for _ in range(2):
            answers = _answers(_status(client, owner, [{"slug": "print-me", "since": _iso(value)} for value in order]).json())
            if "print-me" not in answers:
                continue  # rows of one UTC day can't be told apart: no answer
            assert answers["print-me"] is not None and (
                datetime.fromisoformat(answers["print-me"]).replace(tzinfo=None) < now - timedelta(hours=2)
            )


def test_invalid_batches_are_rejected(client):
    owner = _login(client)
    _create(client, owner)
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
    link = _create(client, owner)
    now = datetime.now(UTC).replace(microsecond=0)
    response = client.post(
        "/api/v1/links/open-status", headers=owner,
        json={"host": "brevl.ink", "since": _iso(now - timedelta(hours=1)),
              "entries": [{"slug": link["slug"]}]},  # no per-entry since
    )
    assert response.status_code == 200, response.text
    assert response.json()["entries"] == [{"slug": link["slug"], "opened_at": None}]


def test_interval_is_bounded_by_seven_days_and_retention(client, monkeypatch):
    from app.core.config import settings

    owner = _login(client)
    _create(client, owner)
    now = datetime.now(UTC)
    entry = {"slug": "print-me", "since": _iso(now - timedelta(days=6, hours=23))}
    assert _status(client, owner, [entry]).status_code == 200
    assert _status(client, owner, [dict(entry, since=_iso(now - timedelta(days=7, minutes=1)))]).status_code == 422
    # A retention shorter than 7 days bounds the interval too: earlier events may be pruned.
    monkeypatch.setattr(settings, "click_event_retention_days", 3)
    assert _status(client, owner, [dict(entry, since=_iso(now - timedelta(days=4)))]).status_code == 422
    assert _status(client, owner, [dict(entry, since=_iso(now - timedelta(days=2)))]).status_code == 200


def test_earlier_day_cannot_hide_a_click_in_the_interval(client, ordinary_rows):
    owner = _login(client)
    _create(client, owner)
    now = datetime.now(UTC).replace(tzinfo=None)
    _seed(client, [now - timedelta(days=1)])
    assert _ask(client, owner, "print-me", now - timedelta(hours=1)) == {"print-me": None}


def test_filtered_bot_and_prefetch_traffic_reads_as_zero(client):
    owner = _login(client)
    _create(client, owner)
    for headers in ({"user-agent": "TelegramBot (like TwitterBot)"}, {"user-agent": "Slackbot-LinkExpanding"}, {"purpose": "prefetch"}):
        assert _redirect(client, **headers).status_code == 307
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    assert _ask(client, owner, "print-me", since) == {"print-me": None}
    assert _redirect(client).status_code == 307
    assert _ask(client, owner, "print-me", since)["print-me"] is not None


def test_missing_foreign_and_inactive_links_are_omitted(client, monkeypatch):
    from app.services import domain_sharing

    monkeypatch.setattr(domain_sharing, "mailer", client.test_mailer)
    owner = _login(client)
    link = _create(client, owner)
    other = _login(client, "open-status-other@example.com")
    domain_id = _domain(client, owner, monkeypatch)
    _create(client, owner, slug="shared", domain_id=domain_id)
    since = _iso(datetime.now(UTC) - timedelta(hours=1))

    def ask(headers, **body):
        slugs = ("print-me", "inactive", "missing", "shared", "print-me")
        response = _status(client, headers, [{"slug": slug, "since": since} for slug in slugs], **body)
        assert response.status_code == 200
        return [entry["slug"] for entry in response.json()["entries"]]

    # A duplicate entry is answered once; the custom-domain link was never
    # clicked; an unowned slug is omitted. Read order follows the batch.
    assert ask(owner) == ["print-me", "shared"]
    assert client.patch(f"/api/v1/links/{link['id']}", headers=owner, json={"is_active": False}).status_code == 200
    assert ask(owner) == ["shared"]
    assert ask(other, host="go.example.com") == []

    # Accepted membership grants the domain, so its links become readable.
    assert client.post(f"/api/v1/domains/{domain_id}/members", headers=owner, json={"email": "open-status-other@example.com"}).status_code == 201
    token = client.test_mailer.messages[-1].text.split("token=")[1].split()[0]
    assert client.post("/api/v1/domains/invites/accept", headers=other, json={"token": token}).status_code == 200
    assert ask(other, host="go.example.com") == ["shared"]
    assert ask(other) == ["shared"]


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
