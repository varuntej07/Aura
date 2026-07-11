"""POST /internal/orchestrate — the reactive orchestrate Cloud Task callback.

Enqueued (coalesced, one per user) by the outbox relay and the inline presence
dispatch. Carries only ``user_id``; the orchestrate pass drains that user's event
inbox itself. Scheduler-token gated in main.py (Cloud Tasks only).
"""

from __future__ import annotations

from typing import Any

from ..services.reactive.orchestrator import run_orchestrate


async def handle_orchestrate(payload: dict[str, Any]) -> dict[str, Any]:
    user_id: str = str(payload.get("user_id", "")).strip()
    if not user_id:
        return {"error": "user_id is required"}
    result = await run_orchestrate(user_id)
    return {"ok": True, **result}
