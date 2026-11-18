"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)(\s*[=:]\s*)([^\s,;\"']+)"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"bot\d{6,}:[A-Za-z0-9_\-]{20,}"),  # Telegram bot token embedded in URLs
]

_registered_secrets: set[str] = set()


def register_secrets(values: list[str]) -> None:
    """Register literal secret values that must never appear in logs or alerts."""
    for value in values:
        if value and len(value) >= 4:
            _registered_secrets.add(value)


def redact(text: str) -> str:
    """Remove secret-looking substrings and registered secret values from ``text``."""
    for secret in _registered_secrets:
        text = text.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
        else:
            text = pattern.sub("***", text)
    return text


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON with redaction."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            payload["ctx"] = json.loads(redact(json.dumps(extra, default=str)))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""
    return logging.getLogger(name)
