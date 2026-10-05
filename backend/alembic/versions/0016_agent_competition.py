"""add learning competition between trading analysts

Revision ID: 0016_agent_competition
Revises: 0015_scale_out_defaults
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0016_agent_competition"
down_revision: str | None = "0015_scale_out_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_performance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="CHALLENGER"),
        sa.Column("observations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_predictions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_predictions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_reward", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ema_reward", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rating", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("agent_name", name="uq_agent_performance_agent_name"),
    )
    op.create_index("ix_agent_performance_agent_name", "agent_performance", ["agent_name"])
    op.create_index("ix_agent_performance_role", "agent_performance", ["role"])
    op.create_index("ix_agent_performance_status", "agent_performance", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_performance_status", table_name="agent_performance")
    op.drop_index("ix_agent_performance_role", table_name="agent_performance")
    op.drop_index("ix_agent_performance_agent_name", table_name="agent_performance")
    op.drop_table("agent_performance")
