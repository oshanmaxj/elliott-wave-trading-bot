import json
import logging
import re
from typing import Any

from app.database.session import SessionLocal
from app.models import BotLog


class SecretRedactionFilter(logging.Filter):
    patterns = [
        re.compile(r'(?i)(api[_-]?key|signature|secret|authorization)(["\'\s:=]+)([^,\s}"\']+)'),
        re.compile(r'(?i)(x-mbx-apikey)(["\'\s:=]+)([^,\s}"\']+)'),
    ]
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        for pattern in self.patterns:
            text = pattern.sub(r'\1\2[REDACTED]', text)
        record.msg, record.args = text, ()
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format='%(asctime)s %(levelname)s %(name)s %(message)s')
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactionFilter())


def log_event(level: str, service: str, event_type: str, message: str, context: dict[str, Any] | None = None) -> None:
    context = context or {}
    logger = logging.getLogger(service)
    getattr(logger, level.lower(), logger.info)("%s %s", message, json.dumps(context, default=str))
    try:
        with SessionLocal.begin() as db:
            db.add(BotLog(level=level.upper(), service=service, event_type=event_type, message=message[:1000], context_json=context))
    except Exception:
        logger.exception("Could not persist bot log")
