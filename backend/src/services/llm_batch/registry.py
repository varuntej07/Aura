"""``job_kind`` -> result-handler registration.

This is the only coupling between the queue and its consumers. A new subsystem
registers a handler at import time and enqueues items; nothing else in this
package needs to know it exists.

A handler receives the original item and one parsed result line and returns
whether it successfully consumed the result. It MUST be idempotent: a crash
between "handler returned" and "item marked dispatched" leaves the item
pollable, so the same result can legitimately arrive twice.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from ...lib.logger import logger
from .client import ResultLine
from .models import BatchItem

BatchResultHandler = Callable[[BatchItem, ResultLine], Awaitable[bool]]

_handlers: dict[str, BatchResultHandler] = {}


def register(job_kind: str, handler: BatchResultHandler) -> None:
    """Register (or replace) the handler for a job kind."""
    if job_kind in _handlers:
        logger.warn("llm_batch.registry: replacing existing handler", {"job_kind": job_kind})
    _handlers[job_kind] = handler


def get_handler(job_kind: str) -> BatchResultHandler | None:
    return _handlers.get(job_kind)


def registered_kinds() -> list[str]:
    return sorted(_handlers)
