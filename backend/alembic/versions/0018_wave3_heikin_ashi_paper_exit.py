"""Add exit_signal_candle_id to paper_forward_trades for signal-driven exits.

Additive only: supports the elliott_wave3_heikin_ashi strategy's 5m
opposite-Heikin-Ashi-reversal exit by recording which candle caused the
exit. No existing table, column or row is modified or removed.

Uses batch mode so the added foreign key also works on SQLite (used by the
test suite), which cannot ALTER an existing table to add a constraint.

Revision ID: 0018
Revises: 0017
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("paper_forward_trades") as batch_op:
        batch_op.add_column(sa.Column("exit_signal_candle_id", sa.Integer, nullable=True))
        batch_op.create_foreign_key(
            "fk_paper_forward_trades_exit_signal_candle_id",
            "candles", ["exit_signal_candle_id"], ["id"], ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_paper_forward_trades_exit_signal_candle_id", ["exit_signal_candle_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("paper_forward_trades") as batch_op:
        batch_op.drop_index("ix_paper_forward_trades_exit_signal_candle_id")
        batch_op.drop_constraint("fk_paper_forward_trades_exit_signal_candle_id", type_="foreignkey")
        batch_op.drop_column("exit_signal_candle_id")
