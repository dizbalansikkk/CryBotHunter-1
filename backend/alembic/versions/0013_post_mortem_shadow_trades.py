"""post-mortem replay and shadow trades

Revision ID: 0013_post_mortem_shadow_trades
Revises: 0012_telegram_outbox_heartbeats
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0013_post_mortem_shadow_trades"
down_revision: str | None = "0012_telegram_outbox_heartbeats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_trades",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("rl_models.id"), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("stop", sa.Float(), nullable=False),
        sa.Column("take", sa.Float(), nullable=False),
        sa.Column("fee", sa.Float(), nullable=False, server_default="0"),
        sa.Column("slippage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("entry_context", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("exit_reason", sa.String(length=32)),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_shadow_trades_model_id", "shadow_trades", ["model_id"])
    op.create_index("ix_shadow_trades_symbol", "shadow_trades", ["symbol"])
    op.create_index("ix_shadow_trades_timeframe", "shadow_trades", ["timeframe"])
    op.create_index("ix_shadow_trades_status", "shadow_trades", ["status"])
    op.create_index("ix_shadow_trades_closed_at", "shadow_trades", ["closed_at"])

    op.create_table(
        "trade_post_mortems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("positions.id"), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=False),
        sa.Column("planned_risk", sa.Float(), nullable=False, server_default="0"),
        sa.Column("result_r", sa.Float(), nullable=False, server_default="0"),
        sa.Column("shaped_reward", sa.Float(), nullable=False, server_default="0"),
        sa.Column("priority", sa.Float(), nullable=False, server_default="1"),
        sa.Column("primary_label", sa.String(length=64), nullable=False),
        sa.Column("behavior_labels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("strategy_followed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("market_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("execution_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("reward_components", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("lessons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("position_id", name="uq_trade_post_mortems_position_id"),
    )
    op.create_index("ix_trade_post_mortems_position_id", "trade_post_mortems", ["position_id"])
    op.create_index("ix_trade_post_mortems_symbol", "trade_post_mortems", ["symbol"])
    op.create_index("ix_trade_post_mortems_priority", "trade_post_mortems", ["priority"])
    op.create_index("ix_trade_post_mortems_primary_label", "trade_post_mortems", ["primary_label"])
    op.create_index("ix_trade_post_mortems_entered_at", "trade_post_mortems", ["entered_at"])
    op.create_index("ix_trade_post_mortems_closed_at", "trade_post_mortems", ["closed_at"])


def downgrade() -> None:
    op.drop_index("ix_trade_post_mortems_closed_at", table_name="trade_post_mortems")
    op.drop_index("ix_trade_post_mortems_entered_at", table_name="trade_post_mortems")
    op.drop_index("ix_trade_post_mortems_primary_label", table_name="trade_post_mortems")
    op.drop_index("ix_trade_post_mortems_priority", table_name="trade_post_mortems")
    op.drop_index("ix_trade_post_mortems_symbol", table_name="trade_post_mortems")
    op.drop_index("ix_trade_post_mortems_position_id", table_name="trade_post_mortems")
    op.drop_table("trade_post_mortems")
    op.drop_index("ix_shadow_trades_closed_at", table_name="shadow_trades")
    op.drop_index("ix_shadow_trades_status", table_name="shadow_trades")
    op.drop_index("ix_shadow_trades_timeframe", table_name="shadow_trades")
    op.drop_index("ix_shadow_trades_symbol", table_name="shadow_trades")
    op.drop_index("ix_shadow_trades_model_id", table_name="shadow_trades")
    op.drop_table("shadow_trades")
