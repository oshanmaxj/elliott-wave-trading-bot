"""Track live-position exchange reconciliation.

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "live_positions",
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("live_positions", sa.Column("exit_reason", sa.String(32), nullable=True))
    op.add_column(
        "live_positions",
        sa.Column("exit_price", sa.Numeric(30, 12), nullable=True),
    )


def downgrade():
    op.drop_column("live_positions", "exit_price")
    op.drop_column("live_positions", "exit_reason")
    op.drop_column("live_positions", "last_reconciled_at")
