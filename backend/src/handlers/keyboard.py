"""
POST /keyboard/draft - the brain of the Buddy Keyboard.

A separate-process keyboard (Android IME / iOS extension) sends the local message
context plus the action the user tapped, and this returns up to N Buddy-voiced
suggestions to insert. Memory-aware actions (reply/continue/rewrite) draft in the
user's voice from their UserAura digest; grammar/translate/tone are pure utility.

Privacy contract (BUDDY_EVERYWHERE.md section 8): this is a memory CONSUMER, never a
producer. Nothing the user typed is persisted; the context is used for the single
requested draft and dropped with the request frame. The funnel event carries only
the action and host app, never the typed content.

Auth: today the keyboard authenticates with the same Firebase ID token the app
holds (resolve_keyboard_uid wraps resolve_user_id_from_request). The dedicated,
revocable keyboard token (M0.2) drops in behind resolve_keyboard_uid without the
drafter ever changing.
"""

from __future__ import annotations

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..services.analytics import funnel_events
from ..services.analytics.posthog_client import capture_event
from ..services.keyboard.drafter import DraftRequest, draft
from ..services.keyboard.vocab import build_vocab_hints
from ..services.request_auth import resolve_user_id_from_request


def resolve_keyboard_uid(request: Request) -> str | None:
    """Resolve the keyboard user.

    M0.2 seam: today this is the app's Firebase ID token (Authorization: Bearer ...),
    plus the non-prod X-Juno-User-Id dev fallback, exactly like every other REST
    endpoint. When the dedicated, revocable keyboard token ships
    (POST /keyboard/token), accept and validate it HERE so the drafter never changes.
    """
    # TODO(M0.2): also accept a dedicated, revocable keyboard token here.
    return resolve_user_id_from_request(request)


async def handle_keyboard_draft(request: Request) -> JSONResponse:
    user_id = resolve_keyboard_uid(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        req = DraftRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse(
            {"error": "Invalid request", "detail": exc.errors()}, status_code=400
        )

    request_id = uuid.uuid4().hex
    result = await draft(user_id, req)

    # Funnel: a draft was served. Fire-and-forget server-side mirror of the client's
    # keyboard_draft_requested step, so the count survives a dropped client capture.
    # Carries ONLY action + host_app — never the typed content (privacy contract).
    await capture_event(
        distinct_id=user_id,
        event=funnel_events.EVENT_KEYBOARD_DRAFT_REQUESTED,
        properties={
            funnel_events.PROP_KEYBOARD_ACTION: req.action,
            funnel_events.PROP_KEYBOARD_HOST_APP: req.host_app or "unknown",
            funnel_events.PROP_KEYBOARD_FIELD_TYPE: req.field_type or "unknown",
        },
    )

    return JSONResponse(
        {
            "suggestions": result.suggestions,
            "reason": result.reason,
            "request_id": request_id,
        },
        status_code=200,
    )


async def handle_keyboard_vocab(request: Request) -> JSONResponse:
    """Return the user's consent-gated vocab hint set (proper-noun-ish known words the keyboard
    caches so it never flags / autocorrects them). Read-only; empty when consent is absent."""
    user_id = resolve_keyboard_uid(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    hints = await build_vocab_hints(user_id)
    return JSONResponse({"tokens": hints.tokens}, status_code=200)
