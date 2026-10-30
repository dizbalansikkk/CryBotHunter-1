"""Persistent daily entry veto, independent of agent decisions."""
from alembic import op
import sqlalchemy as sa

revision = "0019_daily_risk_states"
down_revision = "0018_trading_period_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("daily_risk_states",
        sa.Column("scope", sa.String(80), primary_key=True),
        sa.Column("trading_day", sa.Date(), primary_key=True),
        sa.Column("starting_day_balance", sa.Float(), nullable=False),
        sa.Column("daily_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("tripped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("baseline_source", sa.String(80), nullable=False))


def downgrade():
    op.drop_table("daily_risk_states")
