"""Desktop-reported Guide Mode usage, merged into the user's usage rollup.

The desktop client POSTs one record when an armed Guide Mode window ends. It
carries only what the client can observe (duration, outcome, frame/step/timeout
counts); the voice worker fills in model/TTFT/tools/last-turn on the same rollup
separately. See services/guide_usage_store.py for the merge semantics.
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from ..services.guide_usage_store import record_guide_usage
from ..services.request_auth import resolve_user_id_from_request

_GUIDE_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_VALID_OUTCOMES = frozenset({"completed", "abandoned", "signed_out", "session_ended"})
# Defensive bounds on client-supplied integers (a Guide window is minutes, not
# days; counts are per-session). Keeps a malformed/hostile client from poisoning
# the lifetime counters.
_MAX_DURATION_MS = 24 * 60 * 60 * 1000
_MAX_COUNT = 100_000


def _clamp_int(value: object, *, limit: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, min(value, limit))


def _ended_at_ms(ended_at: str) -> int:
    try:
        parsed = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except Exception:
        # Snapshot guard degrades to "treat as now" rather than rejecting the
        # whole write over an unparseable timestamp.
        return int(datetime.now().timestamp() * 1000)


async def handle_guide_usage(request: Request) -> JSONResponse:
    uid = resolve_user_id_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    guide_session_id = body.get("guide_session_id")
    if not isinstance(guide_session_id, str) or _GUIDE_SESSION_RE.fullmatch(guide_session_id) is None:
        return JSONResponse({"error": "guide_session_id must be a 128-bit hex id."}, status_code=400)

    outcome = body.get("outcome")
    if outcome not in _VALID_OUTCOMES:
        return JSONResponse({"error": "outcome is invalid."}, status_code=400)

    duration_ms = _clamp_int(body.get("duration_ms"), limit=_MAX_DURATION_MS)
    frames_sent = _clamp_int(body.get("frames_sent"), limit=_MAX_COUNT)
    steps_received = _clamp_int(body.get("steps_received"), limit=_MAX_COUNT)
    agent_timeouts = _clamp_int(body.get("agent_timeouts"), limit=_MAX_COUNT)
    if None in (duration_ms, frames_sent, steps_received, agent_timeouts):
        return JSONResponse(
            {"error": "duration_ms/frames_sent/steps_received/agent_timeouts must be integers."},
            status_code=400,
        )

    started_at = body.get("started_at")
    ended_at = body.get("ended_at")
    started_at = started_at[:64] if isinstance(started_at, str) else None
    ended_at = ended_at[:64] if isinstance(ended_at, str) else None

    snapshot_fields = {
        "guide_last_started_at": started_at,
        "guide_last_ended_at": ended_at,
        "guide_last_duration_ms": duration_ms,
        "guide_last_outcome": outcome,
        "guide_last_frames_sent": frames_sent,
        "guide_last_steps_received": steps_received,
        "guide_last_agent_timeouts": agent_timeouts,
    }
    increments = {
        "guide_sessions_count": 1,
        "guide_total_ms": duration_ms,
        "guide_completed_count": 1 if outcome == "completed" else 0,
        "guide_frames_sent_total": frames_sent,
    }

    await record_guide_usage(
        uid,
        guide_session_id=guide_session_id,
        ended_at_ms=_ended_at_ms(ended_at or ""),
        snapshot_fields=snapshot_fields,
        increments=increments,
    )
    # Fail-soft: the store logs its own failures; the client treats any non-2xx
    # as a swallowed blip, so a store hiccup should not surface as a 500.
    return JSONResponse({"ok": True})
