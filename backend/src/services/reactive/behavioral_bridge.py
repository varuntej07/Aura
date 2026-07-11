"""Bridge the client ``/events`` stream into the reactive layer.

The Flutter app already posts taps / dismissals / app opens / content views to
``/events`` (the signal engine's learning input). This bridge taps that same stream
and turns the behavioral ones into reactive state, WITHOUT disturbing the existing
signal-engine ingestion (it runs as a separate fire-and-forget task).

Two outputs:
  * PRESENCE STATE — app_open / tap / dismiss update the presence doc the
    surface-aware Delivery Arbiter reads (foreground + dismiss-streak).
  * THE BUS (forward-compat) — the behavioral event is emitted onto the reactive bus
    ONLY if a registered agent subscribes to it. No agent reacts to a raw app_open
    with a push today (you don't notify someone for opening the app), so this is a
    no-op now; registering a behavioral agent later flips it on with zero producer
    change. This keeps every bus event consumer-backed (no wasted orchestrates).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...lib.logger import logger
from . import event_bus, presence
from .events import (
    EVENT_APP_OPENED,
    EVENT_CONTENT_VIEWED,
    EVENT_NOTIFICATION_DISMISSED,
    EVENT_NOTIFICATION_TAPPED,
)

# Client event_type (signal_events.py) -> reactive event type.
_CLIENT_TO_REACTIVE: dict[str, str] = {
    "app_open": EVENT_APP_OPENED,
    "notification_opened": EVENT_NOTIFICATION_TAPPED,
    "notification_dismissed": EVENT_NOTIFICATION_DISMISSED,
    "content_view": EVENT_CONTENT_VIEWED,
    "content_view_long": EVENT_CONTENT_VIEWED,
    "content_view_short": EVENT_CONTENT_VIEWED,
}

# Reactive types whose dispatch (if subscribed) should be ~immediate (presence).
_PRESENCE_REACTIVE = frozenset({EVENT_APP_OPENED})


async def bridge_client_event(uid: str, event_type: str, *, now: datetime | None = None) -> None:
    """Apply one client event to presence + (if subscribed) the bus. Fire-and-forget;
    never raises into the request path."""
    when = now or datetime.now(UTC)

    # 1) Presence / negative-signal state.
    try:
        if event_type == "app_open":
            await presence.record_app_open(uid, now=when)
        elif event_type == "notification_opened":
            await presence.record_tap(uid, now=when)
        elif event_type == "notification_dismissed":
            await presence.record_dismiss(uid, now=when)
    except Exception as exc:
        logger.warn("behavioral_bridge: presence update failed", {
            "user_id": uid, "event_type": event_type, "error": str(exc),
        })

    # 2) Forward to the bus only if something consumes it.
    reactive_type = _CLIENT_TO_REACTIVE.get(event_type)
    if reactive_type:
        try:
            await event_bus.emit_if_subscribed(
                uid, reactive_type,
                source="client_events",
                presence=reactive_type in _PRESENCE_REACTIVE,
            )
        except Exception as exc:
            logger.warn("behavioral_bridge: bus emit failed", {
                "user_id": uid, "event_type": event_type, "error": str(exc),
            })
