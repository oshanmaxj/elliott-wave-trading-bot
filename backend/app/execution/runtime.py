from sqlalchemy import select

from app.models import BotRuntimeState


def runtime_state(db):
    """Return the canonical singleton runtime-state row."""
    return db.scalar(select(BotRuntimeState).order_by(BotRuntimeState.id).limit(1))
