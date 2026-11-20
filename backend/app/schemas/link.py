"""Link-related Pydantic schemas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from app.core.config import settings

OPEN_STATUS_MAX_DAYS = 7


class LinkCreate(BaseModel):
    url: HttpUrl
    slug: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Custom slug. Randomly generated if omitted.",
    )
    title: str | None = Field(default=None, max_length=256)
    domain_id: str | None = None
    # Opt in to open-status answers for this link; off keeps today's behaviour.
    reliable_open_status: bool = False


class LinkUpdate(BaseModel):
    url: HttpUrl | None = None
    slug: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    title: str | None = Field(default=None, max_length=256)
    is_active: bool | None = None


class LinkOut(BaseModel):
    id: str
    user_id: str
    domain_id: str | None
    slug: str
    url: str
    title: str | None
    is_active: bool
    reliable_open_status: bool = False
    clicks: int
    created_at: datetime
    updated_at: datetime
    short_url: str | None = None  # computed dynamically

    model_config = {"from_attributes": True}


class LinkSummary(BaseModel):
    total_links: int
    total_clicks: int
    active_links: int


class LinkList(BaseModel):
    items: list[LinkOut]
    total: int
    summary: LinkSummary


class OpenStatusEntry(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    # Batch callers send a per-entry interval; a client may also send one
    # `since` for the whole request. The per-entry value wins.
    since: datetime | None = None

    @field_validator("since")
    @classmethod
    def rfc3339_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        # A naive timestamp is rejected rather than assumed: reading a local
        # time as UTC would answer over an interval the caller never asked
        # about, and "no click" is a claim that must not rest on that guess.
        if value.tzinfo is None:
            raise ValueError("since must be an RFC 3339 timestamp with timezone")
        return value.astimezone(UTC)


class OpenStatusRequest(BaseModel):
    host: str | None = Field(default=None, max_length=253)
    since: datetime | None = None
    # The cap is on entries; a real client reporting every alert still in its
    # 7-day window can send slightly more than the 100 it aims for.
    entries: list[OpenStatusEntry] = Field(min_length=1, max_length=500)

    @field_validator("since")
    @classmethod
    def rfc3339_utc(cls, value: datetime | None) -> datetime | None:
        return OpenStatusEntry.rfc3339_utc(value)

    @model_validator(mode="after")
    def bounded_interval(self) -> OpenStatusRequest:
        now = datetime.now(UTC)
        points: dict[str, datetime] = {}
        for entry in self.entries:
            since = self.point(entry)
            if since is None:
                raise ValueError(f"since is required for {entry.slug}")
            # Only the interval that is actually read can be proven complete.
            # A start this link was not served for answers nothing anyway; this
            # only refuses a clock that is plainly ahead, so two clocks a few
            # seconds apart are not an error.
            if since > now + timedelta(minutes=1):
                raise ValueError(f"since is in the future for {entry.slug}")
            if since <= now - timedelta(days=OPEN_STATUS_MAX_DAYS):
                raise ValueError(f"since is older than {OPEN_STATUS_MAX_DAYS} days for {entry.slug}")
            # Events before the retention cutoff may be pruned, so an interval
            # reaching into them cannot be proven complete.
            if since <= now - timedelta(days=settings.click_event_retention_days):
                raise ValueError(f"since is older than click event retention for {entry.slug}")
            # One answer per slug follows whichever entry is read first, so two
            # different intervals for the same slug have no order-independent
            # answer: the caller is told, not served a guess. Exact repeats merge.
            if points.setdefault(entry.slug, since) != since:
                raise ValueError(f"conflicting intervals for {entry.slug}")
        return self

    def point(self, entry: OpenStatusEntry) -> datetime:
        return entry.since or self.since


class OpenStatusResult(BaseModel):
    slug: str
    opened_at: datetime | None


class OpenStatusResponse(BaseModel):
    checked_through: datetime
    entries: list[OpenStatusResult]
