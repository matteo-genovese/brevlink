"""Privacy-minimized redirect events and durable daily totals."""
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LinkClickEvent(Base):
    __tablename__ = "link_click_events"
    __table_args__ = (UniqueConstraint("link_id", "day", "visitor_hash", name="uq_click_event_visitor"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("links.id", ondelete="CASCADE"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    country: Mapped[str | None] = mapped_column(String(2))
    referrer_host: Mapped[str | None] = mapped_column(Text)
    device: Mapped[str] = mapped_column(Text)
    visitor_hash: Mapped[str | None] = mapped_column(Text)
    hits: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class LinkClickUncertainty(Base):
    """An accepted redirect whose click record could not be stored.

    Without this row an unrecorded visit would read as "no click since T", so
    the redirect tries to write it in its own short transaction. A row in an
    interval makes that interval unprovable for the link. It carries no
    visitor data, and occurrences are kept as [first, last].
    """
    __tablename__ = "link_click_uncertainty"

    # Both columns are the key: one marker row per link per second.
    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("links.id", ondelete="CASCADE"), primary_key=True)
    # Truncated to the second: failures in the same second share one row
    # instead of colliding, and later ones add their own.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    # The exact instant of the first marker in that second. It only says when
    # the bucket opened; what a marker bounds is `last_at`.
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The exact instant of the last marker in that second, never rounded: a
    # read bounded before it must not take it for a marker already written.
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class LinkClickDaily(Base):
    __tablename__ = "link_click_daily"
    __table_args__ = (UniqueConstraint("link_id", "day", name="uq_click_daily_link_day"),)

    link_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("links.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    visitors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
