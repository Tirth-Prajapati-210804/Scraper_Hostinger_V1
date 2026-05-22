"""add scrape log diagnostics

Revision ID: 1a2b3c4d5e6f
Revises: 8c9d0e1f2a3b
Create Date: 2026-05-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "1a2b3c4d5e6f"
down_revision = "8c9d0e1f2a3b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scrape_logs", sa.Column("result_reason", sa.String(length=40), nullable=True))
    op.add_column("scrape_logs", sa.Column("raw_offers_found", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("scrape_logs", sa.Column("eligible_offers_found", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("scrape_logs", sa.Column("filtered_by_stop_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("scrape_logs", sa.Column("filtered_by_same_airline", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("scrape_logs", sa.Column("filtered_by_duration", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("scrape_logs", sa.Column("requested_market", sa.String(length=8), nullable=True))
    op.add_column("scrape_logs", sa.Column("requested_currency", sa.String(length=8), nullable=True))
    op.add_column("scrape_logs", sa.Column("detected_currency", sa.String(length=8), nullable=True))

    op.alter_column("scrape_logs", "raw_offers_found", server_default=None)
    op.alter_column("scrape_logs", "eligible_offers_found", server_default=None)
    op.alter_column("scrape_logs", "filtered_by_stop_count", server_default=None)
    op.alter_column("scrape_logs", "filtered_by_same_airline", server_default=None)
    op.alter_column("scrape_logs", "filtered_by_duration", server_default=None)


def downgrade() -> None:
    op.drop_column("scrape_logs", "detected_currency")
    op.drop_column("scrape_logs", "requested_currency")
    op.drop_column("scrape_logs", "requested_market")
    op.drop_column("scrape_logs", "filtered_by_duration")
    op.drop_column("scrape_logs", "filtered_by_same_airline")
    op.drop_column("scrape_logs", "filtered_by_stop_count")
    op.drop_column("scrape_logs", "eligible_offers_found")
    op.drop_column("scrape_logs", "raw_offers_found")
    op.drop_column("scrape_logs", "result_reason")
