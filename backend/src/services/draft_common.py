"""Shared plumbing for the bounded-model-call drafters.

``services/keyboard/drafter.py`` and ``services/outbound_draft/drafter.py``
are deliberate siblings: same coded-reason vocabulary (shipped to the
keyboard/desktop as a wire contract), same hard-timeout ladder around one
provider call, same empty-result-is-a-failure rule. The shared core lives
here; each drafter keeps its own prompts, caps, extra reasons
(``REASON_EMPTY_CONTEXT``; ``REASON_NO_FRAME``/``REASON_INVALID``), and its
own empty-output check (only the caller knows its output shape). Both
modules re-export the three shared reasons under their existing names, so
clients and tests keep reading ``drafter.REASON_*``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable

from ..lib.logger import logger

REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_MODEL_ERROR = "model_error"


async def bounded_model_call(
    model_call: Awaitable[Any],
    *,
    timeout_s: float,
    log_prefix: str,
    log_fields: dict[str, Any],
) -> tuple[Any, str]:
    """Await one provider call under a hard timeout.

    Returns ``(result, REASON_OK)`` or ``(None, coded_reason)``. Loud, never
    silent: timeouts and provider failures are logged with the caller's
    fields (which must never contain typed/drafted content).
    """
    try:
        return await asyncio.wait_for(model_call, timeout=timeout_s), REASON_OK
    except asyncio.TimeoutError:
        logger.warn(f"{log_prefix} timed out", dict(log_fields))
        return None, REASON_TIMEOUT
    except Exception as exc:
        logger.warn(f"{log_prefix} model call failed", {**log_fields, "error": str(exc)})
        return None, REASON_MODEL_ERROR
