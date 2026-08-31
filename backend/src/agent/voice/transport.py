"""One way to put a JSON event on the LiveKit data channel.

Every feature that talks to the client used to build its own envelope, encode
it, publish it `reliable=True`, and wrap that in its own try/except with its own
warn line. There were fourteen of those, half reaching the room through
``get_job_context()`` and half holding an injected ``room``, and only three
applying a publish timeout.

The wire shape is a frozen cross-repo contract (see ECOSYSTEM.md and
architectures/guide-mode.md): Aura-Desktop and the Flutter client parse these
payloads. So this module changes WHO builds the envelope and nothing about what
goes on the wire. ``ensure_ascii`` is a parameter for exactly that reason: the
artifact events have always encoded non-ASCII literally and the control events
have always escaped it, and both byte streams are preserved.

Failure is always soft. A lost data packet costs a card update, a toast, or a
pointer animation; it never costs the spoken reply, so nothing here raises.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from livekit.agents import get_job_context

from ...lib.logger import logger


def current_room() -> Any | None:
    """The live job's room, or None outside a job context.

    Several publishers reach the room this way rather than holding one. The
    lookup itself can raise, and it used to sit inside each caller's try/except,
    so it is folded in here to keep "could not publish" a single outcome.
    """
    try:
        return get_job_context().room
    except Exception:
        return None


async def publish_event_dict(
    room: Any | None,
    event: dict[str, Any],
    *,
    topic: str | None = None,
    timeout_s: float | None = None,
    ensure_ascii: bool = True,
    log_message: str,
    log_fields: dict[str, Any] | None = None,
) -> bool:
    """Publish an already-built envelope. True when it left the worker.

    True means the packet was handed to LiveKit, never that the client rendered
    anything. Delivery proof is a separate concern and lives in
    ``artifact_delivery.py``.
    """
    fields = dict(log_fields or {})
    try:
        if room is None:
            raise RuntimeError("no active LiveKit job context")
        data = json.dumps(event, ensure_ascii=ensure_ascii).encode("utf-8")
        publish = room.local_participant.publish_data(
            data, reliable=True, **({"topic": topic} if topic is not None else {})
        )
        if timeout_s is not None:
            await asyncio.wait_for(publish, timeout=timeout_s)
        else:
            await publish
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Both `error` and `error_type` are emitted because the call sites this
        # replaced were split between the two, and a log query written against
        # either one keeps working.
        logger.warn(
            log_message,
            {**fields, "error": str(exc), "error_type": type(exc).__name__},
        )
        return False


async def publish_client_event(
    room: Any | None,
    event_type: str,
    payload: dict[str, Any],
    *,
    topic: str | None = None,
    timeout_s: float | None = None,
    log_message: str,
    log_fields: dict[str, Any] | None = None,
) -> bool:
    """Publish the standard ``{"type": ..., "payload": ...}`` envelope."""
    return await publish_event_dict(
        room,
        {"type": event_type, "payload": payload},
        topic=topic,
        timeout_s=timeout_s,
        log_message=log_message,
        log_fields=log_fields,
    )


DEFAULT_BOUNDARY_POLL_S = 0.5
DEFAULT_BOUNDARY_MAX_WAIT_S = 15.0


async def await_turn_boundary(
    session: Any,
    *,
    poll_s: float = DEFAULT_BOUNDARY_POLL_S,
    max_wait_s: float = DEFAULT_BOUNDARY_MAX_WAIT_S,
    require_user_idle: bool = False,
) -> bool:
    """Wait for a quiet moment before speaking unprompted. True if one arrived.

    Unsolicited speech (a free-tier warning, an injected screen context, a Guide
    nudge) must not land on top of Buddy mid-sentence or over the user. This
    polls ``agent_state`` rather than awaiting ``AgentSession.wait_for_idle``
    deliberately: the SDK primitive resolves on a different condition, and these
    three callers were tuned against the polling behaviour on a live speech path.
    Swapping the mechanism would be a timing change, not a refactor.

    ``require_user_idle`` additionally waits out ``user_state == "speaking"``,
    which the Guide nudge needs and the other callers deliberately do not.
    """
    waited = 0.0
    while waited < max_wait_s:
        agent_listening = str(getattr(session, "agent_state", "")) == "listening"
        user_quiet = (
            not require_user_idle
            or str(getattr(session, "user_state", "")) != "speaking"
        )
        if agent_listening and user_quiet:
            return True
        await asyncio.sleep(poll_s)
        waited += poll_s
    return str(getattr(session, "agent_state", "")) == "listening"
