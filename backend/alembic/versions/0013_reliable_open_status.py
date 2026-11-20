"""Reliable open-status: opt-in column and accepted clicks that could not be stored.

`reliable_open_status` is off for every existing link, so nothing changes for
them. A link that opts in has its redirect fail (503) when the click record
cannot be stored, unless a marker can be written here: one row per link and
second, carrying no visitor data, that makes any interval containing it
unprovable instead of "no click".
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_reliable_open_status"
down_revision = "0012_default_domain_slug"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "links",
        sa.Column("reliable_open_status", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "link_click_uncertainty",
        sa.Column("link_id", sa.Uuid(), sa.ForeignKey("links.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade():
    op.drop_table("link_click_uncertainty")
    op.drop_column("links", "reliable_open_status")
