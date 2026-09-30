"""add structured context to audit logs

Revision ID: 0014_structured_trade_logs
Revises: 0013_post_mortem_shadow_trades
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0014_structured_trade_logs"
down_revision: str | None = "0013_post_mortem_shadow_trades"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("logs", sa.Column("context", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("logs", "context")
