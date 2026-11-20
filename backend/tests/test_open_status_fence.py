"""A click whose store is still open is not answered as a zero.

Runs the real store and read in one event loop, both on the app's engine and
database (DATABASE_URL: a SQLite file, or a real PostgreSQL). The store's
transaction is held open by the test, not by a timer: it commits only after
the read has had its chance to answer.
"""
import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import tempfile
import uuid

import pytest


def test_application_sqlite_pool_waits_for_an_unwritten_click(client):
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.link import Link
    from app.models.link_stats import LinkClickEvent
    from app.services.link_stats import read_open_status
    from test_link_qr import _login
    from test_open_status import _age_link, _reliable

    owner = _login(client)
    _reliable(client, owner)
    clicked = datetime.now(UTC)
    _age_link(client, "print-me", clicked - timedelta(hours=1))

    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()

        async def store():
            async with async_session() as db:
                link = await db.scalar(select(Link).where(Link.slug == "print-me"))
                await db.execute(Link.__table__.update().where(Link.id == link.id).values(clicks=1))
                ready.set()
                await release.wait()
                db.add(LinkClickEvent(id=uuid.uuid4(), link_id=link.id, occurred_at=clicked,
                                      day=clicked.date(), device="desktop", hits=1))
                await db.commit()

        async def read():
            async with async_session() as db:
                owner_id = await db.scalar(select(Link.user_id).where(Link.slug == "print-me"))
                return await read_open_status(
                    db, owner_id, "brevl.ink", [("print-me", clicked - timedelta(minutes=1))]
                )

        writer = asyncio.create_task(store())
        await ready.wait()
        reader = asyncio.create_task(read())
        try:
            await asyncio.wait_for(asyncio.shield(reader), 0.1)
        except asyncio.TimeoutError:
            pass
        finally:
            release.set()
        await writer
        return await reader

    _, found = client.portal.call(scenario)
    assert found == {"print-me": clicked}


@pytest.fixture()
def app_db(monkeypatch):
    """The app's modules, built on this test's database."""
    url = os.environ.get("LIVE_DATABASE_URL") or f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("JWT_SECRET", "test-secret-that-is-long-enough-for-dev")
    # The app's settings, read once per process: point them here for the
    # migration call (none of the app's engines is opened by this test).
    from app.core.config import settings
    previous, settings.database_url = settings.database_url, url
    yield url
    settings.database_url = previous


def test_a_store_open_at_read_time_is_not_a_zero(app_db, tmp_path):
    # One database per run: a SQLite file, or a schema of its own on Postgres.
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = app_db
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        # The same two things the app's engine does, on this engine: the time
        # helper, and a busy timeout instead of a fast failure.
        from sqlalchemy import event
        from app.core.database import _brev_time
        prepared = set()

        @event.listens_for(engine.sync_engine, "begin")
        def _prepare(connection):
            dbapi_connection = connection.connection
            if dbapi_connection not in prepared:
                prepared.add(dbapi_connection)
                dbapi_connection.create_function("brev_time", 1, _brev_time)
            connection.exec_driver_sql("PRAGMA busy_timeout = 30000")
    if "postgresql" in url:
        import asyncpg
        async def clear():
            connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
            await connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            await connection.close()
        asyncio.run(clear())
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")  # the schema a deployment has
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        from app.models.link import Link
        from app.models.link_stats import LinkClickEvent
        from app.models.user import User
        from app.services import link_stats
        from app.services.links import lock_and_resolve

        owner, link_id = uuid.uuid4(), uuid.uuid4()
        async with sessions() as db:
            db.add(User(id=owner, email=f"{owner}@example.com", password_hash="x"))
            db.add(Link(id=link_id, user_id=owner, slug="held", url="https://example.com/destination",
                        reliable_open_status=True, created_at=datetime.now(UTC) - timedelta(hours=2)))
            await db.commit()

        clicked_at = datetime.now(UTC)
        since = (clicked_at - timedelta(minutes=30)).replace(tzinfo=None)
        opened, release = asyncio.Event(), asyncio.Event()

        async def store():
            # The redirect's own transaction, as record_click runs it: the link
            # lock first, every click row after, and the commit last.
            async with sessions() as db:
                await db.execute(Link.__table__.update().where(Link.id == link_id).values(clicks=Link.clicks + 1))
                db.add(LinkClickEvent(id=uuid.uuid4(), link_id=link_id, occurred_at=clicked_at,
                                      day=clicked_at.date(), device="desktop", hits=1))
                await db.flush()
                opened.set()
                await release.wait()
                await db.commit()

        async def read():
            async with sessions() as db:
                link, at = await lock_and_resolve(db, "brevl.ink", "held")  # waits for the store
                found = await link_stats._first_click_between(db, link, since, link_stats._as_utc(at))
                await db.rollback()
            return found

        writer = asyncio.create_task(store())
        await opened.wait()
        reading = asyncio.create_task(read())
        try:
            await asyncio.wait_for(asyncio.shield(reading), 0.3)
        except asyncio.TimeoutError:
            pass  # waiting for the store's commit, as it must
        release.set()
        await writer
        return await asyncio.wait_for(reading, 10)

    try:
        found = asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())
    assert found is not None, "the click's own store held the lock, and the read still said zero"
