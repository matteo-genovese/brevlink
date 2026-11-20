"""Click recording: no raw IP, user agent, or referrer URL reaches storage."""
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import ipaddress
import re
import uuid
from urllib.parse import urlsplit

import logging

from fastapi import HTTPException, Request
from sqlalchemy import DateTime, Text, bindparam, cast, delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.domain import DomainMember
from app.models.link import Link
from app.models.link_stats import LinkClickDaily, LinkClickEvent, LinkClickUncertainty
from app.services import links as link_service
from app.services.links import (
    _link_to_out,
    get_redirect_link,
    increment_clicks,
    lock_link,
)

BOT = re.compile(r"bot|crawler|spider|slurp|bingpreview|facebookexternalhit|facebot|twitterbot|linkedinbot|whatsapp|telegrambot|discordbot|slackbot|googleother|headlesschrome|lighthouse|monitor|uptimerobot", re.I)


def _device(ua: str) -> str:
    if re.search(r"ipad|tablet|kindle|silk|android(?!.*mobile)", ua, re.I):
        return "tablet"
    if re.search(r"mobile|iphone|ipod|android", ua, re.I):
        return "mobile"
    if re.search(r"windows|macintosh|x11|linux|cros", ua, re.I):
        return "desktop"
    return "other"


def _visitor_hash(request: Request, day) -> str | None:
    ua = request.headers.get("user-agent", "")
    # Proxy headers are accepted only when this deployment trusts its ingress.
    ip = request.headers.get("cf-connecting-ip") if settings.trusted_proxy_headers else None
    ip = ip or (request.client.host if request.client else None)
    try:
        ip = str(ipaddress.ip_address(ip))
    except (ValueError, TypeError):
        return None
    if not ua:
        return None
    salt = hmac.digest(settings.jwt_secret.encode(), f"brev-clicks:{day.isoformat()}".encode(), "sha256")
    return hmac.new(salt, (ip + ua).encode(), hashlib.sha256).hexdigest()[:32]


def _referrer_host(value: str | None) -> str | None:
    try:
        parsed = urlsplit(value or "")
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host:
            return None
        return host.encode("idna").decode().lower()[:253]
    except (ValueError, UnicodeError):
        return None


async def record_click(db: AsyncSession, link: Link, request: Request, now: datetime | None = None) -> bool:
    ua = request.headers.get("user-agent", "")
    if BOT.search(ua) or any(
        "prefetch" in request.headers.get(header, "").lower()
        for header in ("purpose", "sec-purpose")
    ):
        return False
    # Lock/update the link first: concurrent visits serialize all three writes.
    await increment_clicks(db, link)
    # And the click's instant is read after that, with every statement below
    # in the same transaction: an open-status read takes the same lock, so it
    # waits for this commit and never answers a zero for this click. Read
    # before the lock, the instant would precede SQL of a later transaction.
    now = _as_utc(now or datetime.now(UTC))
    country = request.headers.get("cf-ipcountry", "").upper()
    if not re.fullmatch(r"[A-Z]{2}", country) or country in {"XX", "T1"}:
        country = None
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    event = insert(LinkClickEvent).values(
        id=uuid.uuid4(), link_id=link.id, occurred_at=now, day=now.date(),
        country=country, referrer_host=_referrer_host(request.headers.get("referer")),
        device=_device(ua), visitor_hash=_visitor_hash(request, now.date()), hits=1,
    ).on_conflict_do_update(
        index_elements=["link_id", "day", "visitor_hash"],
        set_={"hits": LinkClickEvent.hits + 1},
    ).returning(LinkClickEvent.hits)
    hits = (await db.execute(event)).scalar_one()
    new_visitor = int(hits == 1)
    daily = insert(LinkClickDaily).values(link_id=link.id, day=now.date(), clicks=1, visitors=new_visitor)
    await db.execute(daily.on_conflict_do_update(
        index_elements=["link_id", "day"],
        set_={"clicks": LinkClickDaily.clicks + 1, "visitors": LinkClickDaily.visitors + new_visitor},
    ))
    return True


def _as_utc(moment: datetime) -> datetime:
    # Postgres may hand back a naive value depending on the driver: rows are
    # always written in UTC, so a naive one is UTC too.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _utc(value: datetime | str) -> datetime:
    """The stored instant as aware UTC; anything unreadable is unknown.

    Every writer stores UTC, with or without an offset; never read as local.
    """
    if isinstance(value, datetime):
        return _as_utc(value)
    try:
        # Postgres renders "2026-11-19 10:00:00.123456+00"; RFC 3339 wants it
        # without the space.
        stamp = value.strip().replace(" ", "T", 1) if isinstance(value, str) else value
        return _as_utc(datetime.fromisoformat(stamp)
                       if isinstance(stamp, str) else stamp)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("unreadable stored timestamp") from None


async def record_click_uncertainty(db: AsyncSession, link_id: uuid.UUID, now: datetime | None = None) -> None:
    """Remember that an accepted click could not be recorded. Caller commits.

    The caller must pass a fresh session: the failed analytics transaction may
    be unusable. `now` is the instant the marker was written, after the visit,
    so a click it stands for is at or before it.
    """
    # Serialized with the click and the open-status read, which lock first,
    # and read after the lock: a marker never predates the opportunity it
    # stands for, and no read can miss it while answering a window it covers.
    await lock_link(db, link_id)
    moment = _as_utc(now or datetime.now(UTC))
    # One row per link and second, so markers of the same second share a
    # bucket. The bucket is only the conflict key: the row keeps the exact
    # instants, since a read bounded at :40.700 must not treat a marker
    # written at :40.800 as already there.
    bucket = moment.replace(microsecond=0)
    if db.get_bind().dialect.name == "sqlite":
        # SQLite's max() is the two-value form, but only with real operands:
        # text() of the stored column keeps it, on conflict, the stored value.
        keep = func.max(
            text("link_click_uncertainty.last_at"),
            bindparam("keep_at", moment, DateTime(timezone=True)),
        )
        insert = sqlite_insert
    else:
        # Postgres needs GREATEST: max() there is the aggregate.
        keep = func.greatest(LinkClickUncertainty.last_at, moment)
        insert = pg_insert
    await db.execute(
        insert(LinkClickUncertainty).values(
            link_id=link_id, occurred_at=bucket, first_at=moment, last_at=moment, hits=1,
        ).on_conflict_do_update(
            index_elements=["link_id", "occurred_at"],
            set_={"last_at": keep, "hits": LinkClickUncertainty.hits + 1},
        )
    )


async def read_open_status(
    db: AsyncSession, user_id: uuid.UUID, host: str | None,
    entries: list[tuple[str, datetime]], until: datetime | None = None,
):
    """For each (slug, since), the first accepted click in (since, checked_through].

    Returns the bound actually proven complete and the answers. A None is a
    measured zero, only where it is provable; a slug left out is unknown
    (unreadable here, or its clicks cannot prove an answer) and is never to be
    read as "not opened". A link that did not opt in to open-status is left
    out too: a click of its may be lost without a trace.
    """
    found: dict[str, datetime | None] = {}
    unprovable: set[str] = set()
    answers: dict[tuple[uuid.UUID, datetime], datetime | None] = {}
    # The slug each readable link was asked under, in batch order. A duplicate
    # entry is answered once; two deliveries of the same slug share the slug,
    # but one may have happened before the other, so each keeps its interval.
    eligible: dict[str, tuple[Link, datetime]] = {}
    for slug, since in entries:
        # The answer must not depend on entry order: an unanswered question
        # keeps the slug out of the whole batch.
        if slug in unprovable or slug in eligible:
            continue
        if slug in RESERVED_SLUGS:
            # Caddy and FastAPI consume these paths: no redirect, no clicks.
            unprovable.add(slug)
            continue
        # Whatever the redirect answers this host with. Read, not locked: an
        # entry that fails any check below must take no lock at all.
        link = await get_redirect_link(db, host or "", slug)
        if link is None:
            unprovable.add(slug)
            continue
        # A paused (owner) or flagged (moderation) link is 404 from the
        # redirect no matter what it recorded before, so its recent window
        # measured no visit: answered zeroes would describe a link nobody can open.
        if not link.is_active or link.is_flagged or not link.reliable_open_status:
            unprovable.add(slug)
            continue
        # Same access rule as the authenticated reads: own link, own domain,
        # or a domain shared with this account.
        try:
            readable = link.user_id == user_id or (
                link.domain is not None and await _can_read_domain(db, link, user_id)
            )
        except (HTTPException, SQLAlchemyError) as error:
            # 409 (same slug on two domains) or a failed lookup: no guess.
            logging.getLogger(__name__).warning("Open status lookup failed (%s)", type(error).__name__)
            readable = False
        if not readable:
            unprovable.add(slug)
            continue
        eligible[slug] = (link, since)
    # Every eligible row is locked before anything is read, in id order and
    # not in batch order: two batches asking in opposite orders take the rows
    # in the same order and cannot deadlock on each other. A lock is taken
    # only after an entry has passed every check, so no entry the caller may
    # not read touches a row, not even with a no-op.
    await link_service.lock_links(db, [link.id for link, _ in eligible.values()])
    # One instant for the whole batch, read after the last lock: a click store
    # that had it before is committed and visible, and one that gets it later
    # records a later instant. Every query below is bounded by this one value,
    # so no entry can be read over a window the response does not describe.
    # `until` only lets a test pin that instant; the endpoint never passes it.
    checked_through = _as_utc(until) if until is not None else datetime.now(UTC)
    for slug, (link, since) in eligible.items():
        ask = (link.id, since)
        if ask not in answers:
            try:
                answers[ask] = await _first_click_between(db, link, since, checked_through)
            except (SQLAlchemyError, ValueError) as error:
                logging.getLogger(__name__).warning("Open status read failed (%s)", type(error).__name__)
                answers[ask] = _UNPROVABLE
        found[slug] = answers[ask]
        if answers[ask] is _UNPROVABLE:
            unprovable.add(slug)
    # Returned after the caller commits its reads: a zero that a store was
    # about to contradict is gone by then, and the caller may not report it.
    return _as_utc(checked_through), {slug: when for slug, when in found.items() if slug not in unprovable}


_UNPROVABLE = object()

# Paths Caddy answers before the {slug} redirect (Caddyfile, the `routing`
# snippet): /api/*, /docs, /redoc, /openapi.json, /health, /app and /app/*,
# and the legal pages it serves from the landing app with or without a
# trailing slash (/privacy/, /terms/, ...). A slug with one of these names is
# never served on either host, so a click of it is impossible and must not be
# measured. Other consumed names cannot be a slug: no dot, no slash.
# test_caddy_routing.py keeps the Caddyfile honest, test_open_status_review.py
# reads it and requires this set to cover every exact path it answers.
RESERVED_SLUGS = frozenset({
    "api", "app", "docs", "redoc", "openapi.json", "health",
    # The legal pages, served by landing from the same paths.
    "privacy", "terms", "cookies", "legal", "subprocessors",
    # The deployment's own health probes, by convention on the same path.
    "healthz", "readyz", "livez",
    # No path of the Caddyfile that is checked here: names that would be
    # reserved if such routes were deployed. Kept so a name serving something
    # other than a link is never measured as one.
    "status", "robots.txt", "sitemap.xml", "favicon.ico", "assets", "static",
    "login", "register", "dashboard",
})


async def _can_read_domain(db: AsyncSession, link: Link, user_id: uuid.UUID) -> bool:
    """Same access rule as get_qr_link_by_slug: own domain or accepted member."""
    if link.domain.user_id == user_id:  # loaded with the link by get_redirect_link
        return True
    member = await db.scalar(select(DomainMember.id).where(
        DomainMember.domain_id == link.domain_id,
        DomainMember.user_id == user_id,
        DomainMember.accepted_at.is_not(None),
    ))
    return member is not None


_INSTANT = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)\s*(Z|[+-]\d{2}:\d{2})?\s*$"
)


def _native_instant(column):
    return column


def _normalized(value) -> str | None:
    """One instant, as the fixed form SQLite text comparison orders by time.

    UTC, "YYYY-MM-DDTHH:MM:SS.ffffff+00:00", every part padded: a bare second
    and the same second with a fraction then sort as the same instant. What is
    not a stored UTC instant (nothing writes another) is None: unknown.
    """
    if not isinstance(value, str):
        return None
    found = _INSTANT.match(value)
    if not found:
        return None
    day, clock, zone = found.groups()
    if "." in clock:
        clock = clock.ljust(len("00:00:00.000000"), "0")
    else:
        clock = clock if clock.count(":") == 2 else f"{clock}:00"
        clock += ".000000"
    offset = "+00:00" if zone in (None, "Z") else zone
    return f"{day}T{clock}{offset}"


async def _first_click_between(db: AsyncSession, link: Link, since: datetime, until: datetime):
    """First click in (since, until], or None when provably none, or _UNPROVABLE.

    Every comparison is an order by time made by the engine itself: on
    Postgres the instants are native, on SQLite each is normalized to one
    fixed UTC form first (brev_time, above).
    """
    link_id, start, end = link.id, _as_utc(since), _as_utc(until)
    # No redirect served this slug before it was created, so an earlier start
    # was never measured. Read as text: on a row loaded in this very
    # transaction the Python-side default would hand back now(), not the birth.
    created = (await db.execute(
        select(cast(Link.__table__.c.created_at, Text)).where(Link.id == link_id)
    )).scalar()
    if start < _utc(created):
        return _UNPROVABLE
    sqlite = db.get_bind().dialect.name == "sqlite"
    if sqlite:
        # SQLite binds a datetime in its own form and keeps the column as text:
        # an equality of the same instant can then be false (the forms differ),
        # so each side goes through the one normalization helper and each bound
        # is bound as text.
        def instant(column):
            return func.brev_time(column)

        def at(value):
            return bindparam(None, _normalized(_as_utc(value).isoformat(sep=" ")))
    else:
        instant = _native_instant
        at = lambda value: bindparam(None, value, type_=DateTime(timezone=True))  # noqa: E731

    start_at, end_at = at(start), at(end)

    # The first click the events record inside the window. Read as text: on
    # SQLite a value read through the helper is typed by the helper, not by
    # the column.
    first = (await db.execute(
        select(cast(LinkClickEvent.occurred_at, Text)).where(
            LinkClickEvent.link_id == link_id,
            instant(LinkClickEvent.occurred_at) >= start_at,
            instant(LinkClickEvent.occurred_at) <= end_at,
        ).order_by(instant(LinkClickEvent.occurred_at)).limit(1)
    )).scalar()
    first_at = _utc(first) if first is not None else None
    # A visit leaves one row, dated at its first click of that UTC day; a
    # repeat of the same visitor later that day only raises that row's count.
    # So a row is its visitor's first visit of the day, and a repeat recorded
    # in it has no instant of its own at all.
    #
    # A lost visit is the same shape: a marker stands for a visit at or before
    # its own last instant, and the visitor behind it had their first visit of
    # the day at or before that too -- a row whose count, if the repeat was
    # stored, is above one. Two things can therefore have a visit inside this
    # window without any row dated there:
    #  - a marker from the start on: it may be the first open of the window;
    #  - a repeat: a row dated inside the window whose count is above one, or
    #    one dated before the start that repeats and so may carry a visit
    #    inside it. Only a start after that day's first visit clears the
    #    second: an earlier visit only repeats later, not before.
    # Either makes the window unknown rather than answered: a zero would be
    # false, and a click it also holds may not be the first open.
    inside = (await db.execute(
        select(func.count()).select_from(LinkClickEvent).where(
            LinkClickEvent.link_id == link_id,
            instant(LinkClickEvent.occurred_at) >= start_at,
            instant(LinkClickEvent.occurred_at) <= end_at,
            LinkClickEvent.hits > 1,
        )
    )).scalar() or 0
    if inside:
        return _UNPROVABLE
    lost_inside = (await db.execute(
        select(func.count()).select_from(LinkClickUncertainty).where(
            LinkClickUncertainty.link_id == link_id,
            instant(LinkClickUncertainty.last_at) >= start_at,
            instant(LinkClickUncertainty.last_at) <= end_at,
        )
    )).scalar() or 0
    if lost_inside:
        return _UNPROVABLE
    repeated_before = (await db.execute(
        select(func.count()).select_from(LinkClickEvent).where(
            LinkClickEvent.link_id == link_id,
            instant(LinkClickEvent.occurred_at) < start_at,
            LinkClickEvent.hits > 1,
        )
    )).scalar() or 0
    if repeated_before:
        return _UNPROVABLE
    if first_at is not None:
        # One row dated before the start is its visitor's only visit there. A
        # marker dated before it may have cost a visit later that day, before
        # any click here. A marker dated after it belongs to a visitor whose
        # first visit of the day was at or before it, and a repeat of theirs
        # only raised that row's count: the repeat rule above already covers
        # that. Preferring an omission to a later first open, any marker before
        # a first visit leaves the window without an answer.
        lost_before = (await db.execute(select(instant(LinkClickUncertainty.last_at)).where(
            LinkClickUncertainty.link_id == link_id,
            instant(LinkClickUncertainty.last_at) < start_at,
        ))).scalars().all()
        dated = (await db.execute(select(instant(LinkClickEvent.occurred_at)).where(
            LinkClickEvent.link_id == link_id,
            instant(LinkClickEvent.occurred_at) < start_at,
        ))).scalars().all()
        if any(_utc(marker) < _utc(moment) for marker in lost_before for moment in dated):
            return _UNPROVABLE
        return first_at  # an answer, not a comparison: its exact instant
    # Nothing in the window is dated, and nothing above leaves a visit in it.
    # A marker from before the window stands for one at or before it, which
    # may be this window's first open if it is after that day's first visit:
    # no row then carries its instant. (A marker before that first visit is
    # already covered: it leaves the day's visit unexplained, above.)
    lost_before = (await db.execute(select(instant(LinkClickUncertainty.last_at)).where(
        LinkClickUncertainty.link_id == link_id,
        instant(LinkClickUncertainty.last_at) < start_at,
    ))).scalars().all()
    if lost_before:
        return _UNPROVABLE
    return None


async def prune_click_events(db: AsyncSession, now: datetime | None = None) -> int:
    """Caller commits; daily aggregates are deliberately preserved.

    Uncertainty markers expire by their own age, from the retention cutoff,
    never from the oldest event some other link happens to still have: that
    row says nothing about a click this marker could be hiding.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.click_event_retention_days)
    result = await db.execute(delete(LinkClickEvent).where(LinkClickEvent.occurred_at < cutoff))
    # A marker only ever makes answers unknown. It goes with the raw events of
    # its own second: the marked visit's own rows cannot outlive it, and no
    # window that can still be asked about (at most retention long) reaches back.
    # Derived from the cutoff alone, never from what another link still holds.
    await db.execute(delete(LinkClickUncertainty).where(LinkClickUncertainty.last_at < cutoff))
    return result.rowcount


async def read_stats(db: AsyncSession, user_id: uuid.UUID, days: int, link: Link | None = None):
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days - 1)
    scope = Link.id == link.id if link else Link.user_id == user_id
    daily = (await db.execute(
        select(LinkClickDaily.day, func.sum(LinkClickDaily.clicks), func.sum(LinkClickDaily.visitors))
        .join(Link, Link.id == LinkClickDaily.link_id)
        .where(scope, LinkClickDaily.day >= start, LinkClickDaily.day <= today)
        .group_by(LinkClickDaily.day).order_by(LinkClickDaily.day)
    )).all()
    by_day = {day: (int(clicks), int(visitors)) for day, clicks, visitors in daily}
    series = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        clicks, visitors = by_day.get(day, (0, 0))
        series.append({"day": day.isoformat(), "clicks": clicks, "visitors": visitors})
    result = {
        "range": f"{days}d", "start": start.isoformat(), "end": today.isoformat(),
        "totals": {"clicks": sum(row["clicks"] for row in series), "visitors": sum(row["visitors"] for row in series)},
        "series": series,
        "visitor_definition": "Sum of daily distinct visitors per link; not deduplicated across days or links. Unidentifiable visits count separately.",
        "breakdown_retention_days": settings.click_event_retention_days,
        "breakdown_attribution": "Country, referrer and device of the visitor's first click on that link each UTC day.",
    }
    for name, column in (("countries", LinkClickEvent.country), ("referrers", LinkClickEvent.referrer_host), ("devices", LinkClickEvent.device)):
        rows = (await db.execute(
            select(column, func.sum(LinkClickEvent.hits).label("clicks"))
            .join(Link, Link.id == LinkClickEvent.link_id)
            .where(scope, LinkClickEvent.day >= start, LinkClickEvent.day <= today)
            .group_by(column).order_by(func.sum(LinkClickEvent.hits).desc(), column).limit(10)
        )).all()
        result[name] = [{"name": name, "clicks": int(clicks)} for name, clicks in rows]
    if link:
        result["link"] = _link_to_out(link).model_dump(mode="json")
    else:
        ranks = (await db.execute(
            select(LinkClickDaily.link_id, func.sum(LinkClickDaily.clicks).label("clicks"))
            .join(Link, Link.id == LinkClickDaily.link_id)
            .where(scope, LinkClickDaily.day >= start, LinkClickDaily.day <= today)
            .group_by(LinkClickDaily.link_id).order_by(func.sum(LinkClickDaily.clicks).desc(), LinkClickDaily.link_id).limit(10)
        )).all()
        from sqlalchemy.orm import selectinload
        links = (await db.execute(select(Link).options(selectinload(Link.domain)).where(Link.id.in_([row[0] for row in ranks])))).scalars().all()
        urls = {item.id: _link_to_out(item) for item in links}
        result["top_links"] = [{"slug": urls[id].slug, "short_url": urls[id].short_url, "clicks": int(clicks)} for id, clicks in ranks]
    return result
