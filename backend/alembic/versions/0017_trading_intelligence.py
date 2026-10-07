"""add portfolio telemetry and counterfactual observations

Revision ID: 0017_trading_intelligence
Revises: 0016_agent_competition
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0017_trading_intelligence"
down_revision: str | None = "0016_agent_competition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="BOT_LEDGER"),
        sa.Column("total_equity", sa.Float(), nullable=False),
        sa.Column("free_equity", sa.Float(), nullable=False),
        sa.Column("reserved_equity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("open_stop_risk", sa.Float(), nullable=False, server_default="0"),
        sa.Column("gross_exposure", sa.Float(), nullable=False, server_default="0"),
        sa.Column("net_exposure", sa.Float(), nullable=False, server_default="0"),
        sa.Column("peak_equity", sa.Float(), nullable=False),
        sa.Column("drawdown_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_equity_snapshots_mode", "equity_snapshots", ["mode"])
    op.create_index("ix_equity_snapshots_captured_at", "equity_snapshots", ["captured_at"])

    op.create_table(
        "market_derivative_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("funding_rate", sa.Float()),
        sa.Column("open_interest", sa.Float()),
        sa.Column("open_interest_change_percent", sa.Float()),
        sa.Column("long_short_ratio", sa.Float()),
        sa.Column("liquidation_notional", sa.Float()),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="CCXT_DERIVATIVES"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="UNKNOWN"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_market_derivative_snapshots_symbol", "market_derivative_snapshots", ["symbol"])
    op.create_index("ix_market_derivative_snapshots_captured_at", "market_derivative_snapshots", ["captured_at"])

    op.create_table(
        "signal_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cycle_id", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("signal", sa.String(length=8), nullable=False),
        sa.Column("strategy_score", sa.Integer(), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reference_price", sa.Float(), nullable=False),
        sa.Column("regime", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
        sa.Column("context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("horizon_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome_price", sa.Float()),
        sa.Column("outcome_return_percent", sa.Float()),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for column in ("cycle_id", "symbol", "action", "regime", "horizon_at", "resolved", "observed_at"):
        op.create_index(f"ix_signal_observations_{column}", "signal_observations", [column])

    op.create_table(
        "strategy_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("commit_sha", sa.String(length=64)),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_strategy_releases_version", "strategy_releases", ["version"])
    op.create_index("ix_strategy_releases_commit_sha", "strategy_releases", ["commit_sha"])
    op.create_index("ix_strategy_releases_deployed_at", "strategy_releases", ["deployed_at"])


def downgrade() -> None:
    op.drop_table("strategy_releases")
    op.drop_table("signal_observations")
    op.drop_table("market_derivative_snapshots")
    op.drop_table("equity_snapshots")
