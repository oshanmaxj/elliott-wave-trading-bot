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


def downgrade():
    op.drop_column("live_positions", "last_reconciled_at")
