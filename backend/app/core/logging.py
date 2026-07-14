import json
import logging
from typing import Any

from app.database.session import SessionLocal
from app.models import BotLog


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format='%(asctime)s %(levelname)s %(name)s %(message)s')


def log_event(level: str, service: str, event_type: str, message: str, context: dict[str, Any] | None = None) -> None:
    context = context or {}
    logger = logging.getLogger(service)
    getattr(logger, level.lower(), logger.info)("%s %s", message, json.dumps(context, default=str))
    try:
        with SessionLocal.begin() as db:
            db.add(BotLog(level=level.upper(), service=service, event_type=event_type, message=message[:1000], context_json=context))
    except Exception:
        logger.exception("Could not persist bot log")

