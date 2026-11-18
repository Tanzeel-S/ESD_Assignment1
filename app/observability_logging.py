"""Structured, privacy-safe logging shared by the API and order worker.

Each event is one JSON object on stdout. Docker's json-file driver wraps that
line, and Filebeat unwraps and decodes it into Elasticsearch fields.
"""
import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


class JsonFormatter(logging.Formatter):
    """Emit a stable event schema that Filebeat can decode without grok."""

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "service": os.getenv("SERVICE_NAME", "homeshop"),
            "replica": os.getenv("REPLICA_ID", "-"),
            "severity": record.levelname,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            event["request_id"] = request_id

        # Only explicitly supplied, JSON-serialisable fields are included;
        # never pass request bodies, headers, payment details, or user IDs.
        fields = getattr(record, "event_fields", None)
        if fields:
            event.update(fields)
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, default=str, separators=(",", ":"))


def configure_logging() -> None:
    """Configure the root logger once so framework logs use JSON too."""
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log a named event with searchable fields, excluding sensitive inputs."""
    logger.log(level, message, extra={"event_fields": fields})
