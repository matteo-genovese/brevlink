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
from app.services.links import _link_to_out, get_redirect_link, increment_clicks

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
    # Read the clock before any statement: SQLAlchemy binds Python values when
    # it executes, so a default evaluated there would be later than reported.
    now = _as_utc(now or datetime.now(UTC))
    country = request.headers.get("cf-ipcountry", "").upper()
    if not re.fullmatch(r"[A-Z]{2}", country) or country in {"XX", "T1"}:
        country = None
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    # Lock/update the link first: concurrent visits serialize all three writes.
    await increment_clicks(db, link)
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
    be unusable. `now` is the second the marker was written, after the visit,
    so a click it stands for is at or before it.
    """
    # The clock is read here, not as a default bound when the insert runs.
    moment = _as_utc(now or datetime.now(UTC)).replace(microsecond=0)
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
        insert(LinkClickUncertainty).values(link_id=link_id, occurred_at=moment, last_at=moment, hits=1)
        .on_conflict_do_update(
            index_elements=["link_id", "occurred_at"],
            set_={"last_at": keep, "hits": LinkClickUncertainty.hits + 1},
        )
    )


async def read_open_status(
    db: AsyncSession, user_id: uuid.UUID, host: str | None,
    entries: list[tuple[str, datetime]],
):
    """For each (slug, since), the first accepted click in (since, checked_through].

    Returns the bound actually proven complete and the answers. A None is a
    measured zero, only where it is provable; a slug left out is unknown
    (unreadable here, or its clicks cannot prove an answer) and is never to be
    read as "not opened". A link that did not opt in to open-status is left
    out too: a click of its may be lost without a trace.
    """
    # The fence: first the clock, then a read that sees every store committed
    # before it on this engine, and only then each entry's own window.
    through = datetime.now(UTC)
    await _fence(db)
    found: dict[str, datetime | None] = {}
    unprovable: set[str] = set()
    answers: dict[tuple[uuid.UUID, datetime], datetime | None] = {}
    # Each entry is read over its OWN interval: two deliveries of the same
    # slug share the slug, but one may have happened before the other.
    for slug, since in entries:
        # The answer must not depend on entry order: an unanswered question
        # keeps the slug out of the whole batch.
        if slug in unprovable:
            continue
        # Whatever the redirect answers this host with, for a host nobody
        # serves too: a link the redirect 404s records no click, so "not
        # opened" would promise what the redirect denies (suspended or
        # unverified custom domains, another deployment's links).
        link = await get_redirect_link(db, host or "", slug)
        if link is None or not link.reliable_open_status:
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
        ask = (link.id, since)
        if ask not in answers:
            try:
                answers[ask] = await _first_click_between(db, link, since, through)
            except (SQLAlchemyError, ValueError) as error:
                logging.getLogger(__name__).warning("Open status read failed (%s)", type(error).__name__)
                answers[ask] = _UNPROVABLE
        found[slug] = answers[ask]
        if answers[ask] is _UNPROVABLE:
            unprovable.add(slug)
    return through, {slug: when for slug, when in found.items() if slug not in unprovable}


_UNPROVABLE = object()


async def _fence(db: AsyncSession) -> None:
    """Pin the read to the commits this engine had already made when it ran.

    The window's upper bound is the clock read just before this, and the click
    store stamps its row with the clock read before its insert, so a store that
    commits before this statement carries an instant inside the window, and one
    that has not committed yet is past it and not claimed. That is what lets a
    zero here be a fact rather than a race with a click in flight.

    Postgres keeps one snapshot for a whole statement, SQLite runs it as a
    write transaction. Either statement may abort, and then the link lookup
    that follows fails and nothing is answered.
    """
    await db.execute(select(Link.id).limit(1))


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


async def _first_click_between(db: AsyncSession, link: Link, since: datetime, until: datetime):
    """First click in (since, until], or None when provably none, or _UNPROVABLE.

    Instants are compared by the database: SQLite keeps them as text, so it is
    the engine's own order on both, and a timestamp read back is only ever
    reported, never re-compared in Python.
    """
    link_id, start, end = link.id, _as_utc(since), _as_utc(until)
    # No redirect served this slug before it was created, so an earlier start
    # was never measured. Always read that instant as text: on a row loaded in
    # this very transaction the column's Python-side default would hand back
    # now(), not the stored birth.
    created = (await db.execute(
        select(cast(Link.__table__.c.created_at, Text)).where(Link.id == link_id)
    )).scalar()
    if start < _utc(created):
        return _UNPROVABLE
    first = (await db.execute(
        select(cast(LinkClickEvent.occurred_at, Text)).where(
            LinkClickEvent.link_id == link_id,
            LinkClickEvent.occurred_at >= start,
            LinkClickEvent.occurred_at <= end,
        ).order_by(LinkClickEvent.occurred_at).limit(1)
    )).scalar()
    if first is not None:
        return first  # an answer, not a comparison: its exact instant
    # Nothing in the window is dated. A later visit keeps the first visit's
    # timestamp and only raises that row's count, so it can hide behind a
    # row: not behind one dated before the start with a single visit, and not
    # behind the window's own visit, which is exactly one. A repeat among
    # those, or a count with no row to account for it, leaves something
    # unexplained, so absence of a timestamp stops being proof.
    repeat = (await db.execute(
        select(func.coalesce(func.sum(LinkClickEvent.hits), 0)).where(
            LinkClickEvent.link_id == link_id,
            LinkClickEvent.occurred_at < start,
            LinkClickEvent.hits > 1,
        )
    )).scalar() or 0
    if repeat:
        return _UNPROVABLE
    clicks = 0 if link.clicks is None else link.clicks
    total = (await db.execute(
        select(func.count()).select_from(LinkClickEvent).where(
            LinkClickEvent.link_id == link_id,
        )
    )).scalar() or 0
    # A click being stored right now leaves no row visible here but a count on
    # the link, and reads as that one visit: unknown where it might be a lie,
    # which only ever happens when a read races a store.
    if clicks - total > 1:
        return _UNPROVABLE
    # A gap marker sits at or after the last click it stands for. One near the
    # start may put that click inside the window, so it is not a measured zero.
    if await db.scalar(select(LinkClickUncertainty.link_id).where(
        LinkClickUncertainty.link_id == link_id,
        LinkClickUncertainty.last_at >= start - timedelta(seconds=1),
    ).limit(1)) is not None:
        return _UNPROVABLE
    return None


async def prune_click_events(db: AsyncSession, now: datetime | None = None) -> int:
    """Caller commits; daily aggregates are deliberately preserved.

    Uncertainty markers go only once no raw event is left that they could
    mask, so a marker never dies before the hidden opportunity would.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.click_event_retention_days)
    result = await db.execute(delete(LinkClickEvent).where(LinkClickEvent.occurred_at < cutoff))
    remaining = (await db.execute(select(func.min(LinkClickEvent.occurred_at)))).scalar()
    oldest_event = _as_utc(remaining) if remaining is not None else cutoff
    # A marker is written after the visit it stands for (clock reads are
    # ordered), so one second of slack is enough to keep it while any raw
    # click it could mask may still exist.
    await db.execute(delete(LinkClickUncertainty).where(
        LinkClickUncertainty.last_at < oldest_event - timedelta(seconds=1)
    ))
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
