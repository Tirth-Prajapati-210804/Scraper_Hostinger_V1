"""Add multi-leg open-jaw support to route groups.

`multi_city_legs` holds the ordered EXTRA legs of a multi-city itinerary
(beyond the first origin->destination leg): a JSONB list of 1-3 entries
{"origin": str, "destination": str | "", "nights_before": int}, giving 2-4
total legs. An empty destination on the last entry means "back to the group
origin" (per-origin, like the legacy return leg). `nights_before` = nights
spent at the previous stop, so leg dates derive cumulatively with the same
day-after-last-night rule the legacy return date uses.

NULL = legacy behavior (2-leg open-jaw from special_sheets + nights), so the
column is nullable and the migration is non-destructive.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "4d5e6f7a8b9c"
down_revision = "3c4d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "route_groups",
        sa.Column("multi_city_legs", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("route_groups", "multi_city_legs")
