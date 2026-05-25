"""
Structured JSON logger for backend.

Every log line is a single JSON object with fields:
  timestamp, severity, message, plus any caller-supplied metadata.

Cloud Run ships stdout to Cloud Logging, which auto-parses JSON lines
into searchable structured fields.

Errors logged via logger.exception() include a `traceback` field.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any


def _resolve_level() -> int:
    name = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    return getattr(logging, name, logging.DEBUG)


def _build_stdlib_logger() -> logging.Logger:
    lg = logging.getLogger("juno")
    if lg.handlers:
        return lg
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(handler)
    lg.propagate = False
    lg.setLevel(_resolve_level())
    return lg


_stdlib = _build_stdlib_logger()

_LEVEL_TO_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARN": "WARNING",
    "ERROR": "ERROR",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _emit(level: str, message: str, metadata: dict[str, Any] | None, include_traceback: bool) -> None:
    log_level = getattr(logging, level, logging.DEBUG)
    if not _stdlib.isEnabledFor(log_level):
        return

    record: dict[str, Any] = {
        "timestamp": _now(),
        "severity": _LEVEL_TO_SEVERITY.get(level, level),
        "message": message,
    }
    if metadata:
        record.update(metadata)

    if include_traceback:
        tb = traceback.format_exc()
        if tb and tb.strip() != "NoneType: None":
            record["traceback"] = tb.strip()

    _stdlib.log(log_level, json.dumps(record, default=str))


class Logger:
    """Thin structured logger. Use .exception() inside except blocks for full tracebacks."""

    def debug(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        _emit("DEBUG", message, metadata, False)

    def info(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        _emit("INFO", message, metadata, False)

    def warn(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        _emit("WARN", message, metadata, False)

    def error(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        _emit("ERROR", message, metadata, False)

    def exception(self, message: str, metadata: dict[str, Any] | None = None) -> None:
        """Call from inside an except block — captures and prints the full traceback."""
        _emit("ERROR", message, metadata, True)


logger = Logger()
