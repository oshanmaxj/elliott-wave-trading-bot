"""Deduplicate equivalent execution setups across timeframes.

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "execution_orders",
        sa.Column("setup_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_execution_orders_setup_fingerprint",
        "execution_orders",
        ["setup_fingerprint"],
        unique=True,
    )


def downgrade():
    op.drop_index(
        "ix_execution_orders_setup_fingerprint", table_name="execution_orders"
    )
    op.drop_column("execution_orders", "setup_fingerprint")
