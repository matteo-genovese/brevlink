"""Redirect router - the core {slug} → URL redirect."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session, db_session
from app.services import links as links_service
from app.services.link_stats import record_click, record_click_uncertainty
from app.templates.link_unavailable import LINK_UNAVAILABLE_HTML

router = APIRouter(tags=["redirect"])
logger = logging.getLogger(__name__)

# The page shown when a reliable link cannot record the click. No marketplace
# open happened, so the next answer may still say "not opened".
RECORD_FAILED_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Temporarily unavailable</title></head>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;line-height:1.5">
<h1 style="font-size:1.25rem">The link can't be opened right now</h1>
<p>Please try again in a minute.</p></body></html>
"""


@router.get("/{slug}")
async def redirect(slug: str, request: Request, db: AsyncSession = db_session):
    """Resolve slug and redirect to target URL."""
    link = await links_service.get_redirect_link(db, request.headers.get("host", ""), slug)

    if link is None or not link.is_active or link.is_flagged:
        if "text/html" in request.headers.get("accept", "").lower():
            return HTMLResponse(
                content=LINK_UNAVAILABLE_HTML,
                status_code=status.HTTP_404_NOT_FOUND,
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link not found",
        )

    target_url = link.url
    # The analytics transaction can now take its own connection, including
    # when an in-memory database has a single connection in its pool.
    await db.close()
    outcome = await _record(link, request)
    if outcome is RECORD_FAILED:
        # A reliable link never opens without its click being accounted for:
        # not stored, and the gap not recorded either (the 503 below).
        if "text/html" in request.headers.get("accept", "").lower():
            return Response(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=RECORD_FAILED_HTML,
                media_type="text/html",
                headers={"Cache-Control": "no-store", "Retry-After": "60"},
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The link is temporarily unavailable, please retry",
            headers={"Retry-After": "60", "Cache-Control": "no-store"},
        )
    if outcome is UNCERTAIN:
        logger.critical(
            "Unrecorded click at or before %s on link %s; open-status answers for it stay unknown",
            datetime.now(UTC).isoformat(), link.id,
        )
    return Response(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": target_url},
    )


UNCERTAIN, RECORDED, RECORD_FAILED = "uncertain", "recorded", "failed"


async def _record(link, request: Request) -> str:
    """Store the click, a marker for a gap, or report that neither landed.

    A link that did not opt in to open-status keeps today's behaviour: it is
    redirected even when the click is lost. The marker goes in its own short
    transaction, since the analytics one may be unusable.
    """
    try:
        # Bound analytics latency, including lock waits and commit failures.
        async with asyncio.timeout(1):
            # Isolate analytics failures (including rollback/close) from the
            # read session that resolved the critical redirect.
            async with async_session() as analytics_db:
                # False means filtered traffic (bots, prefetch): not a lost click.
                if not await record_click(analytics_db, link, request):
                    return RECORDED
                await analytics_db.commit()
                return RECORDED
    except Exception as error:
        # Avoid dumping SQL parameters or request data into logs.
        logger.warning("Click recording failed (%s)", type(error).__name__)
    try:
        async with asyncio.timeout(0.5):
            async with async_session() as marker_db:
                await record_click_uncertainty(marker_db, link.id)
                await marker_db.commit()
        return UNCERTAIN
    except Exception as error:
        logger.warning("Unrecorded click marker failed (%s)", type(error).__name__)
        return RECORD_FAILED if link.reliable_open_status else RECORDED
