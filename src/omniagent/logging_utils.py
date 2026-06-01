from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from omniagent.config import LoggingConfig


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
            }:
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(config: LoggingConfig) -> None:
    root = logging.getLogger()
    root.setLevel(config.level.upper())
    for handler in root.handlers:
        handler.close()
    root.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(
        JsonFormatter() if config.json_logs else logging.Formatter("%(levelname)s %(message)s")
    )
    root.addHandler(stream_handler)

    if config.file_path:
        config.file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(Path(config.file_path), encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
