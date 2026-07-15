"""deterministic Elliott Wave counts and points

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(30, 12)
ELLIOTT_SETUP_FK = "fk_trade_setups_elliott_wave_count_id"


def upgrade() -> None:
    op.create_table(
        "elliott_wave_counts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("degree", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("pattern_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("confidence_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("start_candle_id", sa.Integer(), nullable=False),
        sa.Column("end_candle_id", sa.Integer(), nullable=False),
        sa.Column("invalidation_price", PRICE, nullable=False),
        sa.Column("projected_target_min", PRICE, nullable=True),
        sa.Column("projected_target_max", PRICE, nullable=True),
        sa.Column("rules_passed_json", sa.JSON(), nullable=False),
        sa.Column("rules_failed_json", sa.JSON(), nullable=False),
        sa.Column("fibonacci_scores_json", sa.JSON(), nullable=False),
        sa.Column("structure_confirmation_json", sa.JSON(), nullable=False),
        sa.Column("liquidity_confirmation_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["symbol_id"], ["symbols.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["start_candle_id"], ["candles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["end_candle_id"], ["candles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "symbol_id",
            "timeframe",
            "pattern_type",
            "start_candle_id",
            "end_candle_id",
            name="uq_wave_count_equivalent",
        ),
    )
    for column in (
        "symbol_id",
        "timeframe",
        "degree",
        "direction",
        "pattern_type",
        "status",
        "detected_at",
    ):
        op.create_index(
            f"ix_elliott_wave_counts_{column}", "elliott_wave_counts", [column]
        )

    op.create_table(
        "elliott_wave_points",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wave_count_id", sa.Integer(), nullable=False),
        sa.Column("wave_label", sa.String(4), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("swing_point_id", sa.Integer(), nullable=False),
        sa.Column("candle_id", sa.Integer(), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fibonacci_ratio", sa.Numeric(12, 6), nullable=True),
        sa.Column("duration_bars", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["wave_count_id"], ["elliott_wave_counts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["swing_point_id"], ["swing_points.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["candle_id"], ["candles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "wave_count_id", "sequence_number", name="uq_wave_point_sequence"
        ),
        sa.UniqueConstraint("wave_count_id", "wave_label", name="uq_wave_point_label"),
    )
    op.create_index(
        "ix_elliott_wave_points_wave_count_id",
        "elliott_wave_points",
        ["wave_count_id"],
    )

    op.add_column(
        "trade_setups",
        sa.Column("elliott_wave_count_id", sa.Integer(), nullable=True),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("trade_setups") as batch_op:
            batch_op.create_foreign_key(
                ELLIOTT_SETUP_FK,
                "elliott_wave_counts",
                ["elliott_wave_count_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        op.create_foreign_key(
            ELLIOTT_SETUP_FK,
            "trade_setups",
            "elliott_wave_counts",
            ["elliott_wave_count_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("trade_setups") as batch_op:
            batch_op.drop_constraint(ELLIOTT_SETUP_FK, type_="foreignkey")
            batch_op.drop_column("elliott_wave_count_id")
    else:
        op.drop_constraint(ELLIOTT_SETUP_FK, "trade_setups", type_="foreignkey")
        op.drop_column("trade_setups", "elliott_wave_count_id")
    op.drop_table("elliott_wave_points")
    op.drop_table("elliott_wave_counts")
