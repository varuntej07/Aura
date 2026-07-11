"""
POST /desktop/draft-outbound/refine - reworks an existing Buddy Draft.

The desktop overlay's draft card calls this for its refine chips (shorter, longer,
more formal, warmer, regenerate), both during a live voice call and after it ends.
It is text-only by design: the screen frame that produced the draft lives only in
the voice worker's session memory, so a refine operates on the prior draft plus the
model-written context summary the worker shipped to the desktop in draft.created.

There is deliberately NO quota check here. New drafts are metered in the voice
worker (the only place a draft can be minted from a screen); requiring prior_draft
and context_summary makes this endpoint structurally unable to create one, so
refines stay free on every path.

Persistence: when the desktop sends the worker-minted ``draft_id``, a
successful refine overwrites the stored doc at ``UserAura/{uid}/drafts`` so
the dashboard shows the text the user ended up with. Strictly update-only:
a client-supplied id can never MINT a doc (only the voice worker creates
drafts), and a draft the user deleted from the dashboard stays deleted.
Old clients omit the field and behave exactly as before. The funnel event
still carries only channel, length, and the chip kind, never the draft text.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from ..services.analytics import funnel_events
from ..services.analytics.posthog_client import capture_event
from ..services.chat_completion.prompt_builder import fetch_cached_aura_data
from ..services.drafts import store as draft_store
from ..services.outbound_draft.drafter import (
    CONTEXT_SUMMARY_MAX_CHARS,
    HINT_MAX_CHARS,
    PRIOR_DRAFT_MAX_CHARS,
    REASON_OK,
    refine_outbound,
    writing_voice_lines,
)
from ..services.request_auth import resolve_user_id_from_request

# Chip slugs the desktop sends; anything else is a free-form instruction and gets
# reported to analytics as "custom" so the breakdown stays low-cardinality.
_KNOWN_INSTRUCTION_KINDS: frozenset[str] = frozenset(
    {"shorter", "longer", "more_formal", "warmer", "regenerate"}
)


class RefineRequest(BaseModel):
    """Validated body of POST /desktop/draft-outbound/refine."""

    channel: str
    length: str
    prior_draft: str = Field(min_length=1, max_length=PRIOR_DRAFT_MAX_CHARS)
    refine_instruction: str = Field(min_length=1, max_length=HINT_MAX_CHARS)
    context_summary: str = Field(default="", max_length=CONTEXT_SUMMARY_MAX_CHARS)
    # The chip slug when a chip was tapped ("shorter", "warmer", ...); omitted for
    # free-form instructions. Analytics breakdown only, never shapes the draft.
    instruction_kind: str | None = None
    # The worker-minted draft id (uuid4 hex), so a successful refine updates the
    # stored dashboard doc. Optional: old desktop builds omit it, and a bogus id
    # hits the store's update-only-if-exists path, which skips silently.
    draft_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


async def handle_draft_outbound_refine(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        req = RefineRequest.model_validate(body)
    except ValidationError as exc:
        return JSONResponse(
            {"error": "Invalid request", "detail": exc.errors()}, status_code=400
        )

    # Memory is additive: a profile read failure degrades to no-personalization,
    # never a failed refine (fetch_cached_aura_data is consent-gated + cached).
    try:
        profile, _ = await fetch_cached_aura_data(user_id)
        voice_lines = writing_voice_lines(profile)
    except Exception:
        voice_lines = []

    result = await refine_outbound(
        user_id,
        channel=req.channel,
        length=req.length,
        prior_draft=req.prior_draft,
        refine_instruction=req.refine_instruction,
        context_summary=req.context_summary,
        voice_lines=voice_lines,
    )

    if result.reason == REASON_OK and req.draft_id:
        # Update-only-if-exists; the store never raises, so a deleted or
        # unknown draft costs nothing and the refine response is unaffected.
        await draft_store.update_draft_text(
            user_id, req.draft_id, text=result.text, length=req.length
        )

    instruction_kind = (
        req.instruction_kind
        if req.instruction_kind in _KNOWN_INSTRUCTION_KINDS
        else "custom"
    )
    await capture_event(
        distinct_id=user_id,
        event=funnel_events.EVENT_DESKTOP_DRAFT_REFINED,
        properties={
            funnel_events.PROP_DRAFT_CHANNEL: req.channel,
            funnel_events.PROP_DRAFT_LENGTH: req.length,
            funnel_events.PROP_DRAFT_MODE: "rest",
            funnel_events.PROP_DRAFT_INSTRUCTION_KIND: instruction_kind,
        },
    )

    return JSONResponse({"text": result.text, "reason": result.reason}, status_code=200)
