"""Rolling agent outcomes and point-in-time market features."""
from alembic import op
import sqlalchemy as sa
revision = "0020_trust_and_market_features"
down_revision = "0019_daily_risk_states"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("UPDATE rl_models SET is_active = false, status = 'RETIRED' WHERE status IN ('ACTIVE', 'SHADOW')")
    op.add_column("agent_performance", sa.Column("trust_history", sa.JSON(), nullable=False, server_default="[]"))
    op.create_table("market_feature_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False))
    op.create_index("ix_market_feature_snapshots_symbol", "market_feature_snapshots", ["symbol"])
    op.create_index("ix_market_feature_snapshots_available_at", "market_feature_snapshots", ["available_at"])

def downgrade():
    op.drop_table("market_feature_snapshots")
    op.drop_column("agent_performance", "trust_history")
