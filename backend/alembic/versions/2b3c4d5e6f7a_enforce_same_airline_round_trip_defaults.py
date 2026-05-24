"""enforce same-airline round-trip defaults

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-05-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "2b3c4d5e6f7a"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE route_groups "
            "SET trip_type = 'round_trip' "
            "WHERE trip_type = 'one_way'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE route_groups "
            "SET same_airline_only = true "
            "WHERE same_airline_only IS DISTINCT FROM true"
        )
    )
    op.alter_column(
        "route_groups",
        "trip_type",
        existing_type=sa.String(length=20),
        server_default=sa.text("'round_trip'"),
        existing_nullable=False,
    )
    op.alter_column(
        "route_groups",
        "same_airline_only",
        existing_type=sa.Boolean(),
        server_default=sa.true(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "route_groups",
        "trip_type",
        existing_type=sa.String(length=20),
        server_default=sa.text("'one_way'"),
        existing_nullable=False,
    )
    op.alter_column(
        "route_groups",
        "same_airline_only",
        existing_type=sa.Boolean(),
        server_default=sa.false(),
        existing_nullable=False,
    )
