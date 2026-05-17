"""Cascade scrape logs on route group delete

Revision ID: 6a7b8c9d0e1f
Revises: d5e6f7a8b9c0
Create Date: 2026-05-17 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "6a7b8c9d0e1f"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("scrape_logs_route_group_id_fkey", "scrape_logs", type_="foreignkey")
    op.create_foreign_key(
        "scrape_logs_route_group_id_fkey",
        "scrape_logs",
        "route_groups",
        ["route_group_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("scrape_logs_route_group_id_fkey", "scrape_logs", type_="foreignkey")
    op.create_foreign_key(
        "scrape_logs_route_group_id_fkey",
        "scrape_logs",
        "route_groups",
        ["route_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
