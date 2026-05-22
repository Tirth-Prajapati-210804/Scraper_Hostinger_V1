"""Add max leg duration to route groups."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8c9d0e1f2a3b"
down_revision = "7b8c9d0e1f2a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "route_groups",
        sa.Column("max_leg_duration_minutes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("route_groups", "max_leg_duration_minutes")
