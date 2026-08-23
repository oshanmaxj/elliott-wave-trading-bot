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
    repaired = []
    if "exit_reason" not in columns:
        op.add_column(
            "live_positions",
            sa.Column("exit_reason", sa.String(32), nullable=True),
        )
        repaired.append("exit_reason")
    if "exit_price" not in columns:
        op.add_column(
            "live_positions",
            sa.Column("exit_price", sa.Numeric(30, 12), nullable=True),
        )
        repaired.append("exit_price")
    if repaired:
        op.create_table(
            "migration_0013_repairs",
            sa.Column("column_name", sa.String(32), primary_key=True),
        )
        table = sa.table("migration_0013_repairs", sa.column("column_name", sa.String(32)))
        op.bulk_insert(table, [{"column_name": name} for name in repaired])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "migration_0013_repairs" not in inspector.get_table_names():
        return
    repaired = {
        row[0] for row in op.get_bind().execute(
            sa.text("SELECT column_name FROM migration_0013_repairs")
        )
    }
    columns = _live_position_columns()
    for name in ("exit_price", "exit_reason"):
        if name in repaired and name in columns:
            op.drop_column("live_positions", name)
    op.drop_table("migration_0013_repairs")
