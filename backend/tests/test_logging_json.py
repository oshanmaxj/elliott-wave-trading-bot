from datetime import datetime, timezone

from app.core import logging as logging_module


def test_log_event_encodes_datetime_before_json_persistence(monkeypatch):
    captured = {}

    class Context:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def add(self, row): captured["context"] = row.context_json

    monkeypatch.setattr(logging_module.SessionLocal, "begin", lambda: Context())
    logging_module.log_event("INFO", "analysis_backfill", "complete", "done",
                             {"completed_at": datetime(2026, 8, 21, tzinfo=timezone.utc)})
    assert captured["context"]["completed_at"] == "2026-08-21T00:00:00+00:00"
