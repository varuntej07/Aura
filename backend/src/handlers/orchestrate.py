"""POST /internal/orchestrate — the reactive orchestrate Cloud Task callback.

Enqueued (coalesced, one per user) by the outbox relay and the inline presence
dispatch. Carries only ``user_id``; the orchestrate pass drains that user's event
inbox itself. Scheduler-token gated in main.py (Cloud Tasks only).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..services.reactive.events import Event, is_known_event_type
from ..services.reactive.orchestrator import run_orchestrate


async def handle_orchestrate(payload: dict[str, Any]) -> dict[str, Any]:
    user_id: str = str(payload.get("user_id", "")).strip()
    if not user_id:
        return {"error": "user_id is required"}

    transient_events: list[Event] | None = None
    raw_event = payload.get("transient_event")
    if raw_event is not None:
        if not isinstance(raw_event, dict):
            return {"error": "transient_event must be an object"}
        event = Event.from_dict(raw_event)
        if event.uid != user_id:
            return {"error": "transient_event uid does not match user_id"}
        if not is_known_event_type(event.type):
            return {"error": "transient_event type is not registered"}
        transient_events = [event]

    result = await run_orchestrate(user_id, transient_events=transient_events)
    if transient_events and (
        result.get("skipped") == "lease_held" or "error" in result
    ):
        # A persisted outbox event survives a 200 response and is re-swept. A
        # transient clock event exists only in this Cloud Task, so it must receive a
        # retryable response when the orchestrator could not process it.
        raise HTTPException(status_code=503, detail="Reactive tick must be retried")
    return {"ok": True, **result}
