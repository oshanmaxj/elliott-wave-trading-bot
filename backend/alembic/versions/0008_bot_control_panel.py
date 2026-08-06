"""Persistent bot control panel state

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("exchange_accounts", sa.Column("label", sa.String(64)))
    op.add_column("exchange_accounts", sa.Column("encrypted_api_key", sa.String(2048)))
    op.add_column(
        "exchange_accounts", sa.Column("encrypted_api_secret", sa.String(2048))
    )
    op.create_table(
        "bot_runtime_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("automatic_trading_enabled", sa.Boolean, nullable=False),
        sa.Column("manual_approval_required", sa.Boolean, nullable=False),
        sa.Column("pause_new_entries", sa.Boolean, nullable=False),
        sa.Column("kill_switch_enabled", sa.Boolean, nullable=False),
        sa.Column("enabled_symbols_json", sa.JSON, nullable=False),
        sa.Column("enabled_timeframes_json", sa.JSON, nullable=False),
        sa.Column("enabled_strategies_json", sa.JSON, nullable=False),
        sa.Column("strategy_config_json", sa.JSON, nullable=False),
        sa.Column("risk_config_json", sa.JSON, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("started_by", sa.String(32)),
        sa.Column("stopped_by", sa.String(32)),
        sa.Column("last_decision_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("bot_runtime_state")
    op.drop_column("exchange_accounts", "encrypted_api_secret")
    op.drop_column("exchange_accounts", "encrypted_api_key")
    op.drop_column("exchange_accounts", "label")
