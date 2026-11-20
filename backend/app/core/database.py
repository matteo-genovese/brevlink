"""Async SQLAlchemy engine, session factory, and base model."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import re

from fastapi import Depends
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine_kwargs = {"echo": settings.debug}
if settings.database_url.startswith("sqlite"):
    url = make_url(settings.database_url)
    if url.database in (None, "", ":memory:") or url.query.get("mode") == "memory":
        # Keep the in-memory database alive without letting two sessions
        # share the same physical transaction.
        engine_kwargs.update(poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0)
else:
    engine_kwargs.update({
        "pool_size": 5,
        "max_overflow": 10,
        # Dopo un riavvio o un failover del database le connessioni nel pool
        # sono chiuse: senza il controllo la prima richiesta prende 500.
        "pool_pre_ping": True,
        # Ricicla prima che il server chiuda la connessione per inattivita.
        "pool_recycle": 1800,
    })

engine = create_async_engine(settings.database_url, **engine_kwargs)

if settings.database_url.startswith("sqlite"):
    # SQLite keeps a DateTime as text, and its affinity rules can turn a
    # timestamp comparison into a text/real one: brev_time() puts a stored
    # instant or a bound value in one fixed UTC form, so two of them sort by
    # time (see link_stats._normalized). Text that is not an instant is NULL:
    # unknown, and never matching.
    _INSTANT = re.compile(
        r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)\s*(Z|[+-]\d{2}:\d{2})?\s*$"
    )

    def _brev_time(value):
        found = _INSTANT.match(value) if isinstance(value, str) else None
        if found is None:
            return None
        day, clock, zone = found.groups()
        if "." in clock:
            clock = clock.ljust(15, "0")
        else:
            clock = (clock if clock.count(":") == 2 else f"{clock}:00") + ".000000"
        offset = "+00:00" if zone in (None, "Z") else zone
        return f"{day}T{clock}{offset}"

    @event.listens_for(engine.sync_engine, "begin")
    def _prepare_sqlite_transaction(connection):
        # Each transaction waits for a concurrent one instead of failing at
        # once (SQLite's own default is 5 s): a click store and an open-status
        # read serialize on rows, and the read is meant to wait for the store.
        connection.exec_driver_sql("PRAGMA busy_timeout = 30000")
        # The time helper, once per connection: this is the adapter's own
        # path, which SQLAlchemy bridges with greenlet_spawn and syncs right
        # after this event, so the helper is in before the transaction's first
        # statement.
        if not getattr(connection.connection, "_brev_time", False):
            connection.connection.create_function("brev_time", 1, _brev_time)
            connection.connection._brev_time = True


async_session = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency - yields an async DB session."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Commit before sending the response so the next request sees durable writes,
# and commit failures cannot follow an already successful HTTP response.
db_session = Depends(get_db, scope="function")


async def init_db() -> None:
    """Create all tables for development/test bootstrap only."""
    async with engine.begin() as conn:
        from app.models import api_key, auth, billing, domain, link, link_stats, report, subscription, user  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
