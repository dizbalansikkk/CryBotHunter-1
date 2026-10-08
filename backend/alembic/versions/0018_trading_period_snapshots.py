"""Persist three completed trading periods per calendar day.

Revision ID: 0018_trading_period_snapshots
Revises: 0017_trading_intelligence
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_trading_period_snapshots"
down_revision = "0017_trading_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trading_period_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("start_hour", sa.Integer(), nullable=False),
        sa.Column("end_hour", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("report_date", "start_hour", "mode", name="uq_trading_period_mode"),
    )
    op.create_index("ix_trading_period_snapshots_report_date", "trading_period_snapshots", ["report_date"])


def downgrade():
    op.drop_table("trading_period_snapshots")
