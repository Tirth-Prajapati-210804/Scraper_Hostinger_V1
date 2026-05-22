"""add group safeguard state

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-05-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "2b3c4d5e6f7a"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "route_groups",
        sa.Column(
            "consecutive_operational_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "route_groups",
        sa.Column("last_operational_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "route_groups",
        sa.Column("last_auto_pause_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "route_groups",
        sa.Column("last_auto_pause_note", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "collection_runs",
        sa.Column(
            "safeguards",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("route_groups", "consecutive_operational_failures", server_default=None)
    op.alter_column("collection_runs", "safeguards", server_default=None)


def downgrade() -> None:
    op.drop_column("collection_runs", "safeguards")
    op.drop_column("route_groups", "last_auto_pause_note")
    op.drop_column("route_groups", "last_auto_pause_reason")
    op.drop_column("route_groups", "last_operational_failure_at")
    op.drop_column("route_groups", "consecutive_operational_failures")
