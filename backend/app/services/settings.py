from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Setting
from app.schemas.common import RuntimeSettings

SETTING_KEY = "runtime"


def default_runtime_settings() -> RuntimeSettings:
    config = get_settings()
    return RuntimeSettings(enabled_symbols=config.default_symbols, enabled_timeframes=config.default_timeframes)


def get_runtime_settings(db: Session) -> RuntimeSettings:
    row = db.scalar(select(Setting).where(Setting.key == SETTING_KEY))
    return RuntimeSettings.model_validate(row.value_json) if row else default_runtime_settings()


def save_runtime_settings(db: Session, value: RuntimeSettings) -> RuntimeSettings:
    row = db.scalar(select(Setting).where(Setting.key == SETTING_KEY))
    if row:
        row.value_json = value.model_dump()
    else:
        db.add(Setting(key=SETTING_KEY, value_json=value.model_dump()))
    db.commit()
    return value

