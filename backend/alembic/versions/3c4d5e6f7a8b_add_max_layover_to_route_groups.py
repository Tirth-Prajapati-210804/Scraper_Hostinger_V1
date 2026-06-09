"""Add max layover (halt) duration to route groups.

Client rule: layover/halt time should not exceed ~11h (a longer halt makes the
journey impractical). Stored in minutes, nullable (NULL = "Any", no cap), mirrors
max_leg_duration_minutes. Applied server-side via Kayak's layoverdur=-<min> URL
token (proven honored), with a Python post-filter as a safety net.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "3c4d5e6f7a8b"
down_revision = "2b3c4d5e6f7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "route_groups",
        sa.Column("max_layover_minutes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("route_groups", "max_layover_minutes")
