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
from typing import TYPE_CHECKING

from livekit.agents import RunContext, get_job_context

from ...config.settings import settings
from ...lib.logger import logger
from ...services.analytics import funnel_events
from ...services.analytics.posthog_client import capture_event
from ...services.chat_completion.prompt_builder import fetch_cached_aura_data
from ...services.drafts import store as draft_store
from ...services.entitlement import check_and_increment_daily_outbound_draft_usage
from ...services.outbound_draft.drafter import (
    DEFAULT_CHANNEL,
    REASON_OK,
    SNIPPET_CHANNEL,
    OutboundDraftResult,
    draft_outbound,
    refine_outbound,
    writing_voice_lines,
)
from .artifact_contract import (
    artifact_failed_event,
    artifact_generating_event,
    artifact_ready_event,
    new_request_id,
)
from .screen_frames import ScreenFrameStore
from .tool_filler import (
    DRAFT_FILLER_INTERVAL_S,
    DRAFT_STILL_WORKING_DELAY_S,
    DRAFT_STILL_WORKING_PHRASES,
)

if TYPE_CHECKING:
    from .artifact_delivery import ArtifactDeliveryTracker

_DIRECT_SPEECH_TIMEOUT_S = 5.0
_PUBLISH_TIMEOUT_S = 1.5

# What the model speaks when a call can't produce a draft. Each line is a
# complete, natural sentence the TTS reads verbatim.
# There is deliberately NO ask-channel or ask-length line: Buddy can see the
# screen, so the drafter infers both instead of interrogating the user (the
# old email-vs-DM question had no answer for a form field and looped forever).
# No hotkey/eye-icon instruction here: a desktop call auto-captures the screen
# every turn, so the fix is simply to ask again, not to toggle a removed control.
SPOKEN_NO_FRAME = (
    "I didn't catch your screen that time. Ask me again and I'll take a look."
)
SPOKEN_QUOTA = (
    "That's the last of today's free drafts, they reset tomorrow. Want me to "
    "tweak the one we've got instead?"
)
SPOKEN_FAILED = "I couldn't get that draft together, give it another go?"
SPOKEN_NO_CURRENT_DRAFT = (
    "There isn't a current draft to revise. Ask me to create a new draft first."
)
SPOKEN_DRAFT_STARTED = "Yeah, give me a second."
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
        self.delivery: ArtifactDeliveryTracker | None = None


async def run_draft_tool(
    state: DraftOutboundSession,
    screen_frames: ScreenFrameStore | None,
    *,
    operation: str,
    transcript: str,
    run_ctx: RunContext | None = None,
    current_turn_context_id: str = "",
) -> str:
    """Produce or refine the session's draft; returns ONLY the sentence Buddy
    speaks. Never raises: a raised tool call surfaces as a generic error
    mid-voice-turn, so every failure degrades to speech plus a ``draft.failed``
    event the card can render.

    ``run_ctx`` lets the tool own its acknowledgement and filler speech without
    sending draft lifecycle text back through the LLM. That is deliberate:
    ``RunContext.update`` creates an immediate model reply and a later deferred
    reply, which allowed conversational text to leak into the artifact flow.
    With a frame present, every new-draft call reaches this path, so the desktop
    skeleton and a short acknowledgement appear without a clarifying bounce.
    """
    try:
        if operation == "refine":
            if state.current is None:
                return SPOKEN_NO_CURRENT_DRAFT
            return await _refine_current(state, transcript)
        if operation != "new" or not transcript.strip():
            return SPOKEN_FAILED

        return await _draft_new(
            state,
            screen_frames,
            channel=DEFAULT_CHANNEL,
            # The desktop event contract still requires a valid enum. The
            # adaptive on_screen prompt ignores this storage-only default.
            length="medium",
            recipient_hint="",
            intent=transcript,
            run_ctx=run_ctx,
            current_turn_context_id=current_turn_context_id,
        )
    except Exception as exc:
        # Belt and braces: the drafter itself never raises, so this only
        # catches wiring failures (event publish is already fail-soft).
        logger.error("draft_outbound: tool crashed", {
            "user_id": state.user_id, "session_id": state.session_id,
            "error": str(exc),
        })
        await _publish_draft_event(
            artifact_failed_event(
                request_id=new_request_id(),
                artifact_id=state.current.draft_id if state.current else None,
                reason="model_error",
                retryable=True,
            ),
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
    current_turn_context_id: str = "",
) -> str:
    request_id = new_request_id()
    draft_id = uuid.uuid4().hex
    frame = None
    if screen_frames is not None:
        try:
            frame = await screen_frames.fresh_frame(
                current_turn_context_id=current_turn_context_id
            )
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
            artifact_failed_event(
                request_id=request_id,
                artifact_id=draft_id,
                reason="no_frame",
                retryable=True,
            ),
            state=state,
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
                artifact_failed_event(
                    request_id=request_id,
                    artifact_id=draft_id,
                    reason="quota_exceeded",
                    retryable=False,
                ),
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

    await _publish_draft_event(
        artifact_generating_event(
            request_id=request_id,
            artifact_id=draft_id,
            channel=channel,
            length=length,
            mode="new",
            kind="code" if channel == SNIPPET_CHANNEL else "outbound_message",
            title="Snippet" if channel == SNIPPET_CHANNEL else "Draft",
        ),
        state=state,
    )

    # The acknowledgement bypasses the LLM, so it cannot be folded into the
    # generated draft or create a deferred assistant reply when generation ends.
    if run_ctx is not None:
        try:
            await asyncio.wait_for(
                run_ctx.session.say(SPOKEN_DRAFT_STARTED),
                timeout=_DIRECT_SPEECH_TIMEOUT_S,
            )
        except Exception as exc:
            logger.warn("draft_outbound: acknowledgement failed", {
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
            artifact_failed_event(
                request_id=request_id,
                artifact_id=draft_id,
                reason=result.reason,
                retryable=result.reason != "invalid_request",
            ),
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
    delivered = await _publish_draft_event(
        artifact_ready_event(
            request_id=request_id,
            artifact_id=draft_id,
            revision=1,
            kind="code" if channel == SNIPPET_CHANNEL else "outbound_message",
            channel=channel,
            length=length,
            title="Snippet" if channel == SNIPPET_CHANNEL else "Draft",
            body=result.text,
            content_format="code" if channel == SNIPPET_CHANNEL else "plain_text",
            language=None,
            persisted=channel != SNIPPET_CHANNEL,
            context_summary=result.context_summary,
            recipient_hint=recipient_hint,
        ),
        state=state,
    )
    if not delivered:
        return SPOKEN_FAILED
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
    request_id = new_request_id()
    await _publish_draft_event(
        artifact_generating_event(
            request_id=request_id,
            artifact_id=current.draft_id,
            channel=current.channel,
            length=current.length,
            mode="refine",
            kind="code" if current.channel == SNIPPET_CHANNEL else "outbound_message",
            title="Snippet" if current.channel == SNIPPET_CHANNEL else "Draft",
        ),
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
            artifact_failed_event(
                request_id=request_id,
                artifact_id=current.draft_id,
                reason=result.reason,
                retryable=result.reason != "invalid_request",
            ),
            state=state,
        )
        return SPOKEN_FAILED

    current.text = result.text
    current.revision += 1
    delivered = await _publish_draft_event(
        artifact_ready_event(
            request_id=request_id,
            artifact_id=current.draft_id,
            revision=current.revision,
            kind="code" if current.channel == SNIPPET_CHANNEL else "outbound_message",
            channel=current.channel,
            length=current.length,
            title="Snippet" if current.channel == SNIPPET_CHANNEL else "Draft",
            body=current.text,
            content_format="code" if current.channel == SNIPPET_CHANNEL else "plain_text",
            language=None,
            persisted=current.channel != SNIPPET_CHANNEL,
        ),
        state=state,
    )
    if not delivered:
        return SPOKEN_FAILED
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
    event: dict, *, state: DraftOutboundSession
) -> bool:
    """Push a draft event down the data channel for the desktop card. Fail-soft,
    exactly like screen_saves' publisher: a lost event costs a card update,
    never the spoken reply. Log lines carry ids and lengths, never text."""
    payload = event.get("payload") or {}
    artifact = payload.get("artifact")
    display_key = None
    if (
        state.delivery is not None
        and isinstance(artifact, dict)
        and isinstance(artifact.get("body"), str)
        and isinstance(artifact.get("id"), str)
        and isinstance(artifact.get("revision"), int)
    ):
        display_key = state.delivery.expect(artifact["id"], artifact["revision"])
    try:
        room = get_job_context().room
        data = json.dumps(event, ensure_ascii=False).encode("utf-8")
        await asyncio.wait_for(
            room.local_participant.publish_data(data, reliable=True),
            timeout=_PUBLISH_TIMEOUT_S,
        )
        if (
            display_key is not None
            and state.delivery is not None
            and state.delivery.ack_required
        ):
            confirmed = await state.delivery.wait(display_key)
            if not confirmed:
                await asyncio.wait_for(
                    room.local_participant.publish_data(data, reliable=True),
                    timeout=_PUBLISH_TIMEOUT_S,
                )
                confirmed = await state.delivery.wait(display_key)
            if not confirmed:
                logger.warn("draft_outbound: display not confirmed", {
                    "session_id": state.session_id,
                    "user_id": state.user_id,
                    "event": event.get("type"),
                    "draft_id": payload.get("draft_id"),
                })
                return False
        logger.info("draft_outbound: event published", {
            "session_id": state.session_id, "user_id": state.user_id,
            "event": event.get("type"), "draft_id": payload.get("draft_id"),
            "text_chars": len(payload.get("text") or ""),
        })
        return True
    except Exception as exc:
        logger.warn("draft_outbound: event publish failed", {
            "session_id": state.session_id, "user_id": state.user_id,
            "event": event.get("type"), "error": str(exc),
        })
        return False
    finally:
        if display_key is not None and state.delivery is not None:
            state.delivery.release(display_key)
