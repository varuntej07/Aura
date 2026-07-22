"""
``draft_outbound_message`` - the local, in-process voice tool behind Buddy
Drafts ("draft a short reply to this email from Sarah, politely decline").

Why this can't be an MCP tool, same as ``save_screen_item``: MCP tools execute
over HTTP in the main backend process, which never sees the frame bytes.
:class:`ScreenFrameStore` lives only in THIS worker's memory, scoped to one
LiveKit session, so the tool runs here, sends the frame straight into the
model API request via the shared drafter (``services/outbound_draft``), and
publishes ``draft.*`` events to the desktop over the data channel itself.

Persistence contract: the latest version of every draft is written to
``UserAura/{uid}/drafts/{draft_id}`` (``services/drafts/store.py``) right
after its event is published, so the dashboard's Drafts feed shows what the
user ended up with; a 7-day Firestore TTL expires what they never delete.
The SCREEN FRAME itself stays ephemeral - only the draft text, its
model-written context summary, and the recipient hint persist. Logs and
analytics still never carry text (events carry channel/length/mode only).
The text-only REST refine (``handlers/draft_outbound.py``) updates the same
doc when the desktop sends the draft_id.

Metering: a NEW draft charges the free-tier daily counter once; every refine
(here or over REST) is structurally quota-free because only this module's
new-draft branch can mint a draft from a screen.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass

from livekit.agents import RunContext, get_job_context

from ...config.settings import settings
from ...lib.logger import logger
from ...services.analytics import funnel_events
from ...services.analytics.posthog_client import capture_event
from ...services.chat_completion.prompt_builder import fetch_cached_aura_data
from ...services.drafts import store as draft_store
from ...services.entitlement import check_and_increment_daily_outbound_draft_usage
from ...services.outbound_draft.drafter import (
    CHANNELS,
    DEFAULT_CHANNEL,
    LENGTHS,
    REASON_OK,
    SNIPPET_CHANNEL,
    OutboundDraftResult,
    draft_outbound,
    refine_outbound,
    writing_voice_lines,
)
from .screen_frames import ScreenFrameStore
from .tool_filler import (
    DRAFT_FILLER_INTERVAL_S,
    DRAFT_STILL_WORKING_DELAY_S,
    DRAFT_STILL_WORKING_PHRASES,
)

# Time-box the async-tool acknowledgment: ctx.update only hands control back to
# the LLM, so it should return fast; if it ever hangs, the draft must not wait
# on it. Generous because a miss just means no spoken ack, not a failed draft.
_CTX_UPDATE_TIMEOUT_S = 5.0

# What the model speaks when a call can't produce a draft. Each line is a
# complete, natural sentence the TTS reads verbatim, mirroring the voice
# prompt's own phrasing (the control-alt-S line matches the screen-sight note).
# There is deliberately NO ask-channel or ask-length line: Buddy can see the
# screen, so the drafter infers both instead of interrogating the user (the
# old email-vs-DM question had no answer for a form field and looped forever).
SPOKEN_NO_FRAME = (
    "I can't see your screen yet. Hit control alt S, or tap the eye on my "
    "panel, then ask me again."
)
SPOKEN_QUOTA = (
    "That's the last of today's free drafts, they reset tomorrow. Want me to "
    "tweak the one we've got instead?"
)
SPOKEN_FAILED = "I couldn't get that draft together, give it another go?"
SPOKEN_DRAFT_READY = "Done, it's on your screen. Want me to tweak anything?"
SPOKEN_REFINE_READY = "Updated, take a look."


@dataclass
class DraftState:
    """The one draft this session is holding in worker RAM (the desktop card
    keeps its own copy for the REST refine). The latest version is also
    persisted to ``UserAura/{uid}/drafts`` for the dashboard, so a session
    ending no longer ends the draft's server-side life - the 7-day TTL or a
    dashboard delete does."""

    draft_id: str
    channel: str
    length: str
    text: str
    context_summary: str
    recipient_hint: str
    revision: int


class DraftOutboundSession:
    """Per-voice-session draft state + the identity/tier facts the tool needs."""

    def __init__(
        self, *, user_id: str, session_id: str, user_tier: str, display_name: str
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.user_tier = user_tier
        self.display_name = display_name
        self.current: DraftState | None = None


async def run_draft_tool(
    state: DraftOutboundSession,
    screen_frames: ScreenFrameStore | None,
    *,
    channel: str,
    length: str,
    recipient_hint: str,
    intent: str,
    refine_instruction: str,
    run_ctx: RunContext | None = None,
) -> str:
    """Produce or refine the session's draft; returns ONLY the sentence Buddy
    speaks. Never raises: a raised tool call surfaces as a generic error
    mid-voice-turn, so every failure degrades to speech plus a ``draft.failed``
    event the card can render.

    ``run_ctx`` turns the slow new-draft path into an async tool: the first
    ``ctx.update`` releases the LLM to acknowledge immediately (no dead air
    while the expert vision call runs) and ``ctx.with_filler`` breaks any long
    remaining silence. The refine path never calls ``ctx.update``, so it stays a
    synchronous single-utterance turn. With a frame present, every new-draft
    call now reaches this async path (no clarifying-question bounce), so the
    desktop skeleton and the spoken "still on it" filler always show.
    """
    channel = (channel or "").strip()
    length = (length or "").strip()
    recipient_hint = (recipient_hint or "").strip()
    intent = (intent or "").strip()
    refine_instruction = (refine_instruction or "").strip()

    try:
        # Refining the draft we already made this call: no frame, no quota.
        if refine_instruction and state.current is not None:
            return await _refine_current(state, refine_instruction)

        # A refine request with nothing to refine is really a new-draft ask.
        if refine_instruction and not intent:
            intent = refine_instruction

        # No channel (or an unrecognized one) means "just write what's on my
        # screen": fall back to the adaptive on_screen channel instead of asking
        # which kind it is. The drafter reads the frame to decide.
        if channel not in CHANNELS:
            channel = DEFAULT_CHANNEL
        if channel == SNIPPET_CHANNEL:
            # Snippets have no length ladder; "short" keeps the shared
            # DraftState/store/refine contract satisfied and is ignored by the
            # snippet prompt.
            length = "short"
        elif channel != DEFAULT_CHANNEL and length not in LENGTHS:
            # email_reply / cold_dm still want a ladder length. Default rather
            # than ask, so a missing length never bounces the draft. on_screen
            # is skipped here: it infers its own length from the field/context.
            length = "medium"

        return await _draft_new(
            state,
            screen_frames,
            channel=channel,
            length=length,
            recipient_hint=recipient_hint,
            intent=intent,
            run_ctx=run_ctx,
        )
    except Exception as exc:
        # Belt and braces: the drafter itself never raises, so this only
        # catches wiring failures (event publish is already fail-soft).
        logger.error("draft_outbound: tool crashed", {
            "user_id": state.user_id, "session_id": state.session_id,
            "error": str(exc),
        })
        await _publish_draft_event(
            "draft.failed",
            {"draft_id": state.current.draft_id if state.current else None,
             "reason": "model_error"},
            state=state,
        )
        return SPOKEN_FAILED


async def _draft_new(
    state: DraftOutboundSession,
    screen_frames: ScreenFrameStore | None,
    *,
    channel: str,
    length: str,
    recipient_hint: str,
    intent: str,
    run_ctx: RunContext | None = None,
) -> str:
    frame = None
    if screen_frames is not None:
        try:
            frame = await screen_frames.fresh_frame()
        except Exception as exc:
            logger.warn("draft_outbound: fresh_frame failed", {
                "user_id": state.user_id, "session_id": state.session_id,
                "error": str(exc),
            })
    if frame is None and channel != SNIPPET_CHANNEL:
        # Outbound messages respond to something on screen, so no frame is a
        # hard stop. A snippet's spec is the spoken intent; the frame is a
        # best-effort bonus and its absence just means a text-only draft.
        await _publish_draft_event(
            "draft.failed", {"draft_id": None, "reason": "no_frame"}, state=state
        )
        return SPOKEN_NO_FRAME

    # Free-tier daily cap, prod only, charged only on this new-draft path.
    # Fail-open by design (the counter itself returns allowed on infra errors).
    # Snippets are deliberately uncapped: text-only (no expert vision call) and
    # already bounded by the daily voice-minute cap.
    if (
        settings.is_production
        and state.user_tier == "free"
        and channel != SNIPPET_CHANNEL
    ):
        allowed, count = await check_and_increment_daily_outbound_draft_usage(
            state.user_id
        )
        if not allowed:
            await _publish_draft_event(
                "draft.failed", {"draft_id": None, "reason": "quota_exceeded"},
                state=state,
            )
            await capture_event(
                distinct_id=state.user_id,
                event=funnel_events.EVENT_DESKTOP_DRAFT_LIMIT_HIT,
                properties={funnel_events.PROP_DRAFT_CHANNEL: channel},
            )
            logger.info("draft_outbound: free-tier daily cap hit", {
                "user_id": state.user_id, "session_id": state.session_id,
                "count": count,
            })
            return SPOKEN_QUOTA

    draft_id = uuid.uuid4().hex
    await _publish_draft_event(
        "draft.generating",
        {"draft_id": draft_id, "channel": channel, "length": length, "mode": "new"},
        state=state,
    )

    # First update makes this an async tool: the LLM acknowledges in Buddy's
    # voice right now (referencing the request) while the vision call runs, so
    # there's no dead air. Strictly best-effort and time-boxed: an update
    # failure OR hang must never cost the draft, so it degrades to old-style
    # silence instead of falling into the tool's catch-all.
    if run_ctx is not None:
        try:
            await asyncio.wait_for(
                run_ctx.update(
                    f"Started writing the {channel} draft; it will appear as a "
                    "card on the user's screen when ready. Acknowledge in ONE "
                    "short casual line and stop, no questions."
                ),
                timeout=_CTX_UPDATE_TIMEOUT_S,
            )
        except Exception as exc:
            logger.warn("draft_outbound: ctx.update failed or timed out", {
                "user_id": state.user_id, "session_id": state.session_id,
                "error": str(exc),
            })

    # Snippets carry no persona, so skip the profile read entirely.
    voice_lines = [] if channel == SNIPPET_CHANNEL else await _voice_lines(state)

    async def _generate() -> OutboundDraftResult:
        return await draft_outbound(
            state.user_id,
            channel=channel,
            length=length,
            recipient_hint=recipient_hint,
            intent=intent,
            jpeg_base64=(
                base64.b64encode(frame.jpeg_bytes).decode("ascii") if frame else ""
            ),
            jpeg_width=frame.width_px if frame else None,
            jpeg_height=frame.height_px if frame else None,
            voice_lines=voice_lines,
            display_name=state.display_name,
        )

    # The filler speaks "still on it" only if the vision call outlives the dwell.
    # It is strictly best-effort wrapping: draft_outbound never raises and is
    # time-boxed internally, so a filler enter/exit failure is logged and the
    # already-generated draft is KEPT (never discarded by a filler-cleanup
    # error), or generated once without the filler if the wrapper never ran it.
    result: OutboundDraftResult | None = None
    if run_ctx is not None:
        try:
            async with run_ctx.with_filler(
                lambda step: DRAFT_STILL_WORKING_PHRASES[step],
                delay=DRAFT_STILL_WORKING_DELAY_S,
                interval=DRAFT_FILLER_INTERVAL_S,
                max_steps=len(DRAFT_STILL_WORKING_PHRASES),
            ):
                result = await _generate()
        except Exception as exc:
            logger.warn("draft_outbound: filler wrapper failed", {
                "user_id": state.user_id, "session_id": state.session_id,
                "error": str(exc),
            })
    if result is None:
        result = await _generate()

    if result.reason != REASON_OK:
        await _publish_draft_event(
            "draft.failed", {"draft_id": draft_id, "reason": result.reason},
            state=state,
        )
        return SPOKEN_FAILED

    state.current = DraftState(
        draft_id=draft_id,
        channel=channel,
        length=length,
        text=result.text,
        context_summary=result.context_summary,
        recipient_hint=recipient_hint,
        revision=1,
    )
    await _publish_draft_event(
        "draft.created",
        {
            "draft_id": draft_id,
            "revision": 1,
            "channel": channel,
            "length": length,
            "text": result.text,
            "context_summary": result.context_summary,
            "recipient_hint": recipient_hint,
        },
        state=state,
    )
    # Persist AFTER the publish so the card never waits on Firestore. The
    # store never raises; a lost write costs a dashboard row, not the draft.
    await draft_store.create_draft(
        state.user_id,
        draft_id,
        channel=channel,
        length=length,
        text=result.text,
        context_summary=result.context_summary,
        recipient_hint=recipient_hint,
        session_id=state.session_id,
    )
    await capture_event(
        distinct_id=state.user_id,
        event=funnel_events.EVENT_DESKTOP_DRAFT_REQUESTED,
        properties={
            funnel_events.PROP_DRAFT_CHANNEL: channel,
            funnel_events.PROP_DRAFT_LENGTH: length,
            funnel_events.PROP_DRAFT_MODE: "new",
        },
    )
    logger.info("draft_outbound: draft created", {
        "user_id": state.user_id, "session_id": state.session_id,
        "draft_id": draft_id, "channel": channel, "length": length,
        "text_chars": len(result.text),
    })
    return SPOKEN_DRAFT_READY


async def _refine_current(
    state: DraftOutboundSession, refine_instruction: str
) -> str:
    current = state.current
    assert current is not None  # guarded by the caller
    await _publish_draft_event(
        "draft.generating",
        {
            "draft_id": current.draft_id,
            "channel": current.channel,
            "length": current.length,
            "mode": "refine",
        },
        state=state,
    )

    voice_lines = await _voice_lines(state)
    result = await refine_outbound(
        state.user_id,
        channel=current.channel,
        length=current.length,
        prior_draft=current.text,
        refine_instruction=refine_instruction,
        context_summary=current.context_summary,
        voice_lines=voice_lines,
    )
    if result.reason != REASON_OK:
        await _publish_draft_event(
            "draft.failed",
            {"draft_id": current.draft_id, "reason": result.reason},
            state=state,
        )
        return SPOKEN_FAILED

    current.text = result.text
    current.revision += 1
    await _publish_draft_event(
        "draft.updated",
        {
            "draft_id": current.draft_id,
            "revision": current.revision,
            "length": current.length,
            "text": current.text,
        },
        state=state,
    )
    # Update-only: if the user deleted this draft from the dashboard mid-call
    # (or its create write failed), the store logs and skips - never resurrects.
    await draft_store.update_draft_text(
        state.user_id,
        current.draft_id,
        text=current.text,
        length=current.length,
    )
    await capture_event(
        distinct_id=state.user_id,
        event=funnel_events.EVENT_DESKTOP_DRAFT_REFINED,
        properties={
            funnel_events.PROP_DRAFT_CHANNEL: current.channel,
            funnel_events.PROP_DRAFT_LENGTH: current.length,
            funnel_events.PROP_DRAFT_MODE: "voice",
            funnel_events.PROP_DRAFT_INSTRUCTION_KIND: "voice",
        },
    )
    logger.info("draft_outbound: draft refined", {
        "user_id": state.user_id, "session_id": state.session_id,
        "draft_id": current.draft_id, "revision": current.revision,
        "text_chars": len(current.text),
    })
    return SPOKEN_REFINE_READY


async def _voice_lines(state: DraftOutboundSession) -> list[str]:
    """The consent-gated writing-voice digest; a read failure degrades to the
    drafter's default voice, never a failed draft."""
    try:
        profile, _ = await fetch_cached_aura_data(state.user_id)
        return writing_voice_lines(profile) if profile else []
    except Exception as exc:
        logger.warn("draft_outbound: aura digest read failed", {
            "user_id": state.user_id, "error": str(exc),
        })
        return []


async def _publish_draft_event(
    event_type: str, payload: dict, *, state: DraftOutboundSession
) -> None:
    """Push a draft event down the data channel for the desktop card. Fail-soft,
    exactly like screen_saves' publisher: a lost event costs a card update,
    never the spoken reply. Log lines carry ids and lengths, never text."""
    try:
        room = get_job_context().room
        data = json.dumps({"type": event_type, "payload": payload}).encode("utf-8")
        await room.local_participant.publish_data(data, reliable=True)
        logger.info("draft_outbound: event published", {
            "session_id": state.session_id, "user_id": state.user_id,
            "event": event_type, "draft_id": payload.get("draft_id"),
            "text_chars": len(payload.get("text") or ""),
        })
    except Exception as exc:
        logger.warn("draft_outbound: event publish failed", {
            "session_id": state.session_id, "user_id": state.user_id,
            "event": event_type, "error": str(exc),
        })
