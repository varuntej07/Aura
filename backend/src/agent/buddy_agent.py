"""
BuddyAgent — the persona that drives the LiveKit voice session.

Most tools are exposed via MCP at /mcp (see backend/src/handlers/mcp.py) —
those run over HTTP in the main backend process. The one exception is
``save_screen_item`` below: a LOCAL ``@function_tool``, declared directly on
this class instead, because it needs synchronous access to this session's
in-memory ``ScreenFrameStore`` (the screenshot never leaves this process — see
``voice/screen_saves.py`` for why an MCP tool structurally cannot reach it).
LiveKit's ``Agent`` auto-discovers ``@function_tool``-decorated methods on
``self`` (``find_function_tools``), merging them with the MCP-provided tools
into one tool list for the model — no separate registration needed. Lifecycle:

* on_enter -> open with one casual greeting from CASUAL_GREETINGS, spoken
              verbatim. The opener never references history; what Buddy knows
              about the user surfaces mid-conversation only when relevant (see
              the "Using what you know" section of the voice prompt).

Slow-tool filler phrases are spoken from ``llm_node`` below: a tool call
surfacing in the LLM stream is the only pre-execution signal on this stack, so
``ToolFillerSpeaker`` (voice/tool_filler.py) fires there and speaks once the
turn is committed (agent_state == "thinking"), which is exactly while the tool
is executing. Session events cannot do this (see tool_filler.py's docstring).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterable

from livekit import agents
from livekit.agents import Agent, ModelSettings, RunContext, function_tool
from livekit.agents import llm as lk_llm

from ..lib.logger import logger
from ..services.analytics.llm_telemetry import start_tool_span
from .voice.draft_outbound import DraftOutboundSession, run_draft_tool
from .voice.emotion_tags import convert_audio_cue_stream
from .voice.point_tag import PointTarget, filter_point_tags, publish_element_point
from .voice.screen_frames import ScreenFrameStore, attach_screen_frame_to_turn
from .voice.screen_saves import save_screen_item as _save_screen_item
from .voice.text_sanitizer import sanitize_text_stream, strip_nonverbal_cue_stream
from .voice.tool_filler import ToolFillerSpeaker
from .voice_prompt import VOICE_PROMPT

# A repeated (title, collection_name) tool call inside this window is treated
# as a double-fire (the model re-emitting a call it already made this turn),
# not a second save. Keyed on the raw args the model sent, before collection-
# name dedup resolves them, since that's what would actually repeat.
_DUPLICATE_SAVE_WINDOW_S = 6.0

# Spoken verbatim as the opener, picked at random. Every line is a safe, warm
# hello on its own, so the greeting never depends on a stale memory or last-session
# summary being relevant. Kept lowercase/contracted to read naturally through TTS.
CASUAL_GREETINGS = [
    "whatsup buddy",
    "what's going on",
    "Yooo!! what's good",
    "Heyyy, what's happening",
    "how you doin",
    "hey, what's up",
    "how's it going buddy",
    "what's new with you",
    "hey you, sup?",
]


class BuddyAgent(agents.Agent):
    def __init__(
        self,
        *,
        user_id: str,
        context_vars: dict[str, str],
        chat_ctx: lk_llm.ChatContext,
        screen_frames: ScreenFrameStore | None = None,
        session_id: str = "",
        user_tier: str = "free",
        display_name: str = "",
    ) -> None:
        super().__init__(
            instructions=VOICE_PROMPT.format(**context_vars),
            chat_ctx=chat_ctx,
        )
        self._user_id = user_id
        self._screen_frames = screen_frames
        self._session_id = session_id
        # The frame injected into the current turn; element.point events carry
        # its id so the client maps coordinates against the right geometry.
        self._last_injected_frame_id = ""
        self._point_publish_tasks: set[asyncio.Task] = set()
        # (title, collection_name) -> monotonic call time, for the
        # save_screen_item duplicate-fire guard below.
        self._recent_screen_saves: dict[tuple[str, str], float] = {}
        # Buddy Drafts session state (the one live draft + tier for metering).
        self._draft_outbound = DraftOutboundSession(
            user_id=user_id,
            session_id=session_id,
            user_tier=user_tier,
            display_name=display_name,
        )
        # Built lazily in llm_node: self.session only exists once the agent
        # is active, which is guaranteed there.
        self._tool_filler_speaker: ToolFillerSpeaker | None = None

    async def on_enter(self) -> None:
        await self.session.say(random.choice(CASUAL_GREETINGS))

    async def on_user_turn_completed(
        self, turn_ctx: lk_llm.ChatContext, new_message: lk_llm.ChatMessage
    ) -> None:
        """Attach the desktop's screen frame to the turn when screen sight is armed.

        A session with no frames passes through untouched, which keeps preemptive
        generation intact (see screen_frames.py for why that matters). The helper
        never raises; a raised hook would drop the whole turn reply.
        """
        if self._screen_frames is not None:
            frame = await attach_screen_frame_to_turn(
                self._screen_frames,
                turn_ctx,
                new_message,
                session_id=self._session_id,
                user_id=self._user_id,
            )
            self._last_injected_frame_id = frame.frame_id if frame else ""

    @function_tool
    async def save_screen_item(
        self,
        title: str,
        collection_name: str,
        description: str = "",
        note: str = "",
        source_url: str | None = None,
    ) -> str:
        """Save the thing on screen the user just asked Buddy to remember.

        Call this ONLY when the user explicitly asks to save/remember/bookmark
        something visible on their screen right now ("save these shoes",
        "remember this recipe", "keep this for later") — never speculatively,
        and never for something with no visual referent (use a reminder or
        memory tool instead for those). Persists the current screen-sight
        frame plus what you saw, so the user can revisit it later from their
        dashboard.

        Args:
            title: Short name for the thing being saved, e.g. "Nike Air Max 270".
            collection_name: A short grouping label you invent from context, e.g.
                "Shoes" or "Sister's birthday ideas". Free-form — near-duplicate
                names you've used before ("kicks" vs "Shoes") are merged automatically,
                so just say what feels natural; don't try to reuse an exact past label.
            description: Optional longer detail about what's visible, e.g.
                "black/white, size 10 shown".
            note: Optional — the user's own words about why, e.g. "I like these".
            source_url: Only if a URL is actually visible on screen (e.g. in a
                browser address bar) — never guess or infer one.
        """
        now = time.monotonic()
        dedup_key = (title.strip().casefold(), collection_name.strip().casefold())
        last_call = self._recent_screen_saves.get(dedup_key)
        if last_call is not None and (now - last_call) < _DUPLICATE_SAVE_WINDOW_S:
            return "Already saved that."
        self._recent_screen_saves[dedup_key] = now

        # Local @function_tool, so it bypasses ToolExecutor's telemetry span —
        # record it here to keep the ops tool-analytics complete.
        span = start_tool_span(tool_name="save_screen_item", source="voice", uid=self._user_id)
        try:
            result = await _save_screen_item(
                uid=self._user_id,
                session_id=self._session_id,
                screen_frames=self._screen_frames,
                title=title,
                collection_name=collection_name,
                description=description,
                note=note,
                source_url=source_url,
            )
        except Exception as exc:
            span.finish(success=False, error_type=type(exc).__name__)
            raise
        span.finish()
        return result.spoken_confirmation

    @function_tool
    async def draft_outbound_message(
        self,
        ctx: RunContext,
        channel: str,
        length: str = "",
        recipient_hint: str = "",
        intent: str = "",
        refine_instruction: str = "",
    ) -> str:
        """The ONLY way to produce text the user will copy from you: a reply
        to an email on their screen, a DM/message to a person visible on their
        screen, or a snippet (a terminal command, code, or a config line they
        will copy and run). NEVER write, dictate, spell out, or speak such text
        yourself, not even a rough version; call this instead, the moment they
        ask you to draft, write, or compose one. The draft appears as a card on
        their screen with a copy button; it is never sent or run anywhere.

        Snippets need no screenshot: their spoken request is the spec, so call
        this even when you can't see their screen. Snippets also need no
        length, leave it empty.

        For email_reply and cold_dm, call it immediately even if details are
        missing. If the user hasn't said how long it should be, leave length
        empty; this then returns the exact one-line question to ask them, and
        you call it again with their answer. Every return value from this tool
        is a complete, natural sentence to say to the user.

        When the user asks to CHANGE the draft you already made this call
        ("make it warmer", "mention the deadline", "use setx instead"), call
        this again with ONLY refine_instruction set; leave the other arguments
        empty.

        The user reads the full draft on screen, so never recite it: confirm
        in a few words and offer to tweak it.

        Args:
            channel: "email_reply" when they're replying to an email visible on
                screen; "cold_dm" for a first-touch message to a person/profile
                visible on screen; "snippet" for a terminal command, code,
                config, or any text they'll copy and run verbatim.
            length: "short", "medium", or "detailed", exactly what the user
                chose. Leave empty if they haven't said, and always empty for
                snippet.
            recipient_hint: Who it's for, in the user's words, e.g. "Sarah" or
                "this recruiter". Empty for snippet.
            intent: What they want it to say or do, in their words, e.g.
                "politely decline" or "make PowerShell always open in
                MobileApps".
            refine_instruction: ONLY when changing the existing draft: the
                user's change request, e.g. "warmer" or "make it one line".
        """
        # Local @function_tool, so it bypasses ToolExecutor's telemetry span —
        # record it here (the drafter's own LLM calls are traced in ModelProvider).
        span = start_tool_span(tool_name="draft_outbound_message", source="voice", uid=self._user_id)
        try:
            spoken_reply = await run_draft_tool(
                self._draft_outbound,
                self._screen_frames,
                channel=channel,
                length=length,
                recipient_hint=recipient_hint,
                intent=intent,
                refine_instruction=refine_instruction,
                run_ctx=ctx,
            )
        except Exception as exc:
            span.finish(success=False, error_type=type(exc).__name__)
            raise
        span.finish()
        return spoken_reply

    async def llm_node(
        self,
        chat_ctx: lk_llm.ChatContext,
        tools: list,
        model_settings: ModelSettings,
    ):
        """Strip [POINT:...] tags from the reply stream before ANY consumer sees them.

        llm_node output feeds TTS, the client captions, and the recorded
        transcript, so this single interception keeps the tag out of all three.
        The first coordinate tag per reply publishes an element.point event the
        desktop overlay animates. Sessions without screen sight pass through
        the same filter as a cheap no-op (no '[' in normal speech).
        """
        published = False

        def _on_point(target: PointTarget) -> None:
            nonlocal published
            if published:
                return  # one pointer per reply; extra tags are stripped silently
            published = True
            task = asyncio.create_task(
                publish_element_point(
                    target,
                    frame_id=self._last_injected_frame_id,
                    session_id=self._session_id,
                    user_id=self._user_id,
                ),
                name=f"voice-point-{self._session_id[:8]}",
            )
            self._point_publish_tasks.add(task)
            task.add_done_callback(self._point_publish_tasks.discard)

        stream = self._speak_filler_on_tool_calls(
            Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        )
        async for item in filter_point_tags(stream, on_point=_on_point):
            yield item

    async def _speak_filler_on_tool_calls(self, chunks):
        """Pass-through tee over the raw LLM stream that triggers tool fillers.

        A named tool call in a ChatChunk is the only signal that exists before
        the framework executes the tool, so this is where the slow-tool filler
        fires (see voice/tool_filler.py for the timing-safety rules). Chunks are
        yielded untouched. Filler bookkeeping is wrapped so a filler bug can
        NEVER break the reply stream: on any error we just stop trying to speak
        fillers for the rest of this reply and keep relaying chunks.
        """
        async for item in chunks:
            try:
                self._maybe_fire_tool_filler(item)
            except Exception as exc:
                logger.warn("VoiceSession: tool filler tee failed", {
                    "session_id": self._session_id, "user_id": self._user_id,
                    "error": str(exc),
                })
            yield item

    def _maybe_fire_tool_filler(self, item: object) -> None:
        """Fire the slow-tool filler for any named tool call in ``item``.

        Lazily builds the speaker (self.session only exists once the agent is
        active, which is guaranteed here). Kept separate so the tee's guard wraps
        both construction and the per-call trigger.
        """
        tool_calls = getattr(getattr(item, "delta", None), "tool_calls", None) or []
        if not tool_calls:
            return
        if self._tool_filler_speaker is None:
            self._tool_filler_speaker = ToolFillerSpeaker(
                session=self.session,
                session_id=self._session_id,
                user_id=self._user_id,
            )
        for call in tool_calls:
            name = getattr(call, "name", "")
            if name:
                self._tool_filler_speaker.speak_for_tool(name)

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ):
        """Strip markdown, then turn bracket audio cues into sonic-3 markup, before Cartesia.

        gpt-4.1-mini frequently emits bold/bullets/headers on a voice call; without this,
        TTS reads the markup literally ("asterisk asterisk content"). The sanitizer is
        deterministic and fail-open (see voice/text_sanitizer.py), and flushes per sentence
        so synthesis stays incremental. convert_audio_cue_stream then converts allowlisted
        bracket cues ([excited], [whisper], ...) into inline <emotion/speed/volume> markup
        sonic-3 understands, keeps [laughter] verbatim (the one real Cartesia nonverbalism),
        and strips hallucinated cues like [soft laughter] so they never reach TTS as dead
        air (see voice/emotion_tags.py). We then delegate to the default TTS node.

        It is the transcription path (not this one) that hides every bracket cue
        from the caption; the fallback TTS engines strip this markup themselves
        (voice/fallback_tts_wrapper.py).
        """
        cleaned = convert_audio_cue_stream(sanitize_text_stream(text))
        async for frame in Agent.default.tts_node(self, cleaned, model_settings):
            yield frame

    async def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ):
        """Hide [laughter]-style cues from the client caption and forwarded transcript.

        The reply text forks here (captions/transcript) and to tts_node (audio)
        off the SAME llm_node output. A non-verbal cue like [laughter] must reach
        TTS so Cartesia laughs, but showing the literal "[laughter]" on screen is
        the bug the user hit (with the unsupported "[soft laughter]" it was pure
        dead text). Stripping it on this branch only keeps the laugh audible while
        the caption stays clean. Streaming holdback catches a cue split across
        chunks (see text_sanitizer.strip_nonverbal_cue_stream).
        """
        stripped = strip_nonverbal_cue_stream(text)
        async for chunk in Agent.default.transcription_node(self, stripped, model_settings):
            yield chunk
