"""Widen destination columns for combined multi-airport round-trip search.

Round-trip groups with multiple destination airports now do ONE combined Kayak
search per origin (e.g. YOW-ORY,CDG) and save the cheapest under the COMBINED
destination key "ORY,CDG" (see route_segments._combined_destination). This
migration ONLY widens daily_cheapest_prices.destination (8 -> 64) and
all_flight_results.destination (10 -> 64) so combined keys with several airports
(e.g. "ORY,LHR,CDG") fit instead of overflowing the insert.

The old per-airport rows ("ORY", "CDG" separately) are deliberately KEPT -- the
client wants that history preserved and will delete it themselves later. To stop
those rows showing up as duplicate/stale dates next to fresh combined rows, the
READ paths (prices API + export) filter multi-destination round-trip groups to
their combined key instead (route_segments.combined_destination_for_group).
NO DATA IS DELETED here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "5e6f7a8b9c0d"
down_revision = "4d5e6f7a8b9c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Widen the destination columns to hold a combined comma-joined key.
    op.alter_column(
        "daily_cheapest_prices",
        "destination",
        existing_type=sa.String(length=8),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "all_flight_results",
        "destination",
        existing_type=sa.String(length=10),
        type_=sa.String(length=64),
        existing_nullable=False,
    )

    # NOTE: deliberately NO row cleanup. The legacy per-airport rows for
    # multi-destination round-trip groups are kept in the DB (client will remove
    # them when they choose); the read paths filter them out of display instead.


def downgrade() -> None:
    # Narrow the columns back. This will FAIL if any combined key longer than the
    # old limit exists -- intentional, so a downgrade can't silently truncate data.
    op.alter_column(
        "all_flight_results",
        "destination",
        existing_type=sa.String(length=64),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.alter_column(
        "daily_cheapest_prices",
        "destination",
        existing_type=sa.String(length=64),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
