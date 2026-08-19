"""Repair live-position exit fields missing from deployed revision 0012.

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _live_position_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("live_positions")}


def upgrade():
    columns = _live_position_columns()
    if "exit_reason" not in columns:
        op.add_column(
            "live_positions",
            sa.Column("exit_reason", sa.String(32), nullable=True),
        )
    if "exit_price" not in columns:
        op.add_column(
            "live_positions",
            sa.Column("exit_price", sa.Numeric(30, 12), nullable=True),
        )


def downgrade():
    columns = _live_position_columns()
    if "exit_price" in columns:
        op.drop_column("live_positions", "exit_price")
    if "exit_reason" in columns:
        op.drop_column("live_positions", "exit_reason")
