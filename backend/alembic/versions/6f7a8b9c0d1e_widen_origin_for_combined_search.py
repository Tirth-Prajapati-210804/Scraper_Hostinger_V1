"""Widen origin columns for combined multi-airport searches.

Multi-origin route groups save fresh rows under a comma-combined origin key
(for example "GLA,PIK"). Multi-city groups also use that stored key while the
collector searches each concrete origin separately and saves only the cheapest
winner per date.

This migration only widens route key columns so combined keys with several
airports fit. It deliberately keeps all existing rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "6f7a8b9c0d1e"
down_revision = "5e6f7a8b9c0d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "daily_cheapest_prices",
        "origin",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "all_flight_results",
        "origin",
        existing_type=sa.String(length=10),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "scrape_logs",
        "origin",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "scrape_logs",
        "destination",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Narrowing back will fail if longer combined keys exist, which is safer than
    # silently truncating route identity data.
    op.alter_column(
        "scrape_logs",
        "destination",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "scrape_logs",
        "origin",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    op.alter_column(
        "all_flight_results",
        "origin",
        existing_type=sa.String(length=64),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.alter_column(
        "daily_cheapest_prices",
        "origin",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
