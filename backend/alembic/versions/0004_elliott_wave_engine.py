"""deterministic Elliott Wave counts and points

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

from app.models import ElliottWaveCount, ElliottWavePoint

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ElliottWaveCount.__table__.create(bind=bind, checkfirst=True)
    ElliottWavePoint.__table__.create(bind=bind, checkfirst=True)
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("trade_setups")
    }
    if "elliott_wave_count_id" not in columns:
        op.add_column(
            "trade_setups",
            sa.Column(
                "elliott_wave_count_id",
                sa.Integer(),
                sa.ForeignKey("elliott_wave_counts.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("trade_setups")
    }
    if "elliott_wave_count_id" in columns:
        op.drop_column("trade_setups", "elliott_wave_count_id")
    ElliottWavePoint.__table__.drop(bind=bind, checkfirst=True)
    ElliottWaveCount.__table__.drop(bind=bind, checkfirst=True)
