"""safe scale-out defaults

Revision ID: 0015_scale_out_defaults
Revises: 0014_structured_trade_logs
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0015_scale_out_defaults"
down_revision: str | None = "0014_structured_trade_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing settings are deliberately preserved: changing an operator's
    # configured risk fraction during a deployment would be an unsafe surprise.
    op.alter_column("settings", "partial_close_percent", server_default="25")
    op.alter_column("positions", "partial_close_percent", server_default="25")


def downgrade() -> None:
    op.alter_column("positions", "partial_close_percent", server_default="50")
    op.alter_column("settings", "partial_close_percent", server_default="50")
