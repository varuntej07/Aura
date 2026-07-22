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

from ..config.settings import settings
from ..lib.logger import logger
from ..services.analytics.llm_telemetry import start_tool_span
from ..services.memory.retrieval import (
    VOICE_RETRIEVAL_BUDGET_S,
    render_relevant_memory_block,
    retrieve_relevant_subgraph,
    should_retrieve_for_message,
)
from .voice.action_policy import (
    TurnCapabilityPolicy,
    derive_turn_policy,
    evaluate_execution,
)
from .voice.action_telemetry import VoiceActionTelemetry
from .voice.capabilities import VOICE_TOOL_REGISTRY, ToolEffect, VoiceSurface, tool_name
from .voice.context_compaction import VoiceContextCompactor
from .voice.draft_outbound import DraftOutboundSession, run_draft_tool
from .voice.emotion_tags import convert_audio_cue_stream
from .voice.greeting import resolve_opener
from .voice.point_tag import PointTarget, filter_point_tags, publish_element_point
from .voice.screen_frames import ScreenFrameStore, attach_screen_frame_to_turn
from .voice.screen_saves import save_screen_item as _save_screen_item
from .voice.text_sanitizer import sanitize_text_stream, strip_nonverbal_cue_stream
from .voice.tool_filler import ToolFillerSpeaker
from .voice.tool_skills import instructions_for_skill_names
from .voice.visible_artifacts import present_visible_artifact as _present_visible_artifact
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
        launch_surface: str = "app",
        opener_task: "asyncio.Task[str] | None" = None,
    ) -> None:
        voice_surface = VoiceSurface(launch_surface)
        session_skill_names = [
            registration.skill_name
            for registration in VOICE_TOOL_REGISTRY.values()
            if voice_surface in registration.allowed_surfaces and registration.skill_name
        ]
        instructions = VOICE_PROMPT.format(**context_vars) + instructions_for_skill_names(
            session_skill_names
        )
        super().__init__(
            instructions=instructions,
            chat_ctx=chat_ctx,
        )
        self._user_id = user_id
        self._screen_frames = screen_frames
        self._session_id = session_id
        self._launch_surface = voice_surface
        # Memory-seeded opener racing the static greeting (see voice/greeting.py).
        self._opener_task = opener_task
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
        self._finalized_message_id = ""
        self._finalized_transcript = ""
        self._finalized_policy: TurnCapabilityPolicy | None = None
        self._fresh_frame_for_turn = False
        self._action_telemetry = VoiceActionTelemetry(
            session_id=session_id, surface=self._launch_surface.value
        )
        self._context_compactor = VoiceContextCompactor(session_id=session_id)
        self._context_compaction_checks: set[asyncio.Task] = set()

    async def on_enter(self) -> None:
        # Prefer the memory-seeded opener when it resolves inside the budget;
        # otherwise the static list keeps the sub-1s hello. resolve_opener is
        # fail-open ("" on timeout/error), so the greeting can never hang.
        opener = await resolve_opener(
            self._opener_task, settings.VOICE_GREETING_SEED_BUDGET_S
        )
        await self.session.say(opener or random.choice(CASUAL_GREETINGS))

    async def on_user_turn_completed(
        self, turn_ctx: lk_llm.ChatContext, new_message: lk_llm.ChatMessage
    ) -> None:
        """Finalize action state and attach a desktop frame when screen sight is armed.

        Screen attachment is a no-op when no frame is available. The finalized-turn
        marker still invalidates speculative generation so action safety uses complete
        speech. The frame helper never raises; a raised hook would drop the whole reply.
        """
        compacted_context = self._context_compactor.apply_ready(turn_ctx)
        if compacted_context is None:
            compacted_context = self._context_compactor.enforce_hard_ceiling(turn_ctx)
        context_was_compacted = compacted_context is not None
        if compacted_context is not None:
            turn_ctx.items[:] = compacted_context.items
        finalized_transcript = new_message.text_content
        await self._append_live_graph_context(turn_ctx, finalized_transcript)
        frame = None
        if self._screen_frames is not None:
            frame = await attach_screen_frame_to_turn(
                self._screen_frames,
                turn_ctx,
                new_message,
                session_id=self._session_id,
                user_id=self._user_id,
            )
            self._last_injected_frame_id = frame.frame_id if frame else ""
        self._action_telemetry.start_turn()
        self._fresh_frame_for_turn = frame is not None
        self._finalized_message_id = new_message.id
        self._finalized_transcript = finalized_transcript
        current_turn_index = self._action_telemetry.turn_index
        policy = derive_turn_policy(
            finalized_transcript,
            turn_ctx,
            self._launch_surface,
            self._fresh_frame_for_turn,
            source_message_id=new_message.id,
            turn_index=current_turn_index,
        )
        self._finalized_policy = policy
        # Speculative inference cannot expose side-effect tools. Always invalidate it
        # when final STT arrives so the one Buddy model sees the complete request and
        # the stable finalized tool set. No transcript classifier decides this boundary.
        turn_ctx.add_message(
            role="system",
            content=["<voice_action_turn>finalized transcript available</voice_action_turn>"],
        )
        logger.info(
            "VoiceLatency: preemptive generation decision",
            {
                "session_id": self._session_id,
                "turn_index": current_turn_index,
                "reusable": False,
                "exposed_tool_count": len(policy.allowed_tools),
                "fresh_frame": frame is not None,
                "context_compacted": context_was_compacted,
            },
        )
        if context_was_compacted:
            await self.update_chat_ctx(turn_ctx)

    async def _append_live_graph_context(
        self,
        turn_ctx: lk_llm.ChatContext,
        transcript: str,
    ) -> bool:
        """Attach query-relevant graph memory to this turn only. Never raises."""
        if not should_retrieve_for_message(transcript):
            return False
        try:
            memories = await asyncio.wait_for(
                retrieve_relevant_subgraph(
                    self._user_id,
                    transcript,
                    budget_s=VOICE_RETRIEVAL_BUDGET_S,
                ),
                timeout=VOICE_RETRIEVAL_BUDGET_S,
            )
            block = render_relevant_memory_block(memories)
            if not block:
                return False
            turn_ctx.add_message(role="system", content=[block])
            return True
        except Exception as exc:
            logger.warn(
                "VoiceSession: live graph retrieval failed open",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return False

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
        channel: str = "",
        length: str = "",
        recipient_hint: str = "",
        intent: str = "",
        refine_instruction: str = "",
    ) -> str:
        """Write any text the user wants for whatever is on their screen: an
        email reply, a DM, a form or application field, a comment, a bio, a
        post, a review. You can SEE their screen, so read it to know what to
        write and where it goes; do not ask which kind it is or how long it
        should be, just write it. The draft appears as a card with a copy
        button and is never sent automatically. Never speak the text you
        wrote. Runnable commands, code, config, and prompts belong in
        present_visible_artifact instead. A draft is never a substitute for a
        real action: when the user asks to create a calendar event, a
        reminder, or a tracker, call that action tool instead.

        Call it immediately with whatever the user gave you; every argument is
        optional and inferred from the screen when absent. Every return value
        from this tool is a complete, natural sentence to say to the user.

        When the user asks to CHANGE the draft you already made this call
        ("make it warmer", "mention the deadline", "shorter"), call this again
        with ONLY refine_instruction set; leave the other arguments empty.

        The user reads the full draft on screen, so never recite it, not even
        a preview: confirm in a few words and offer to tweak it.

        Args:
            channel: Optional hint. Leave empty and Buddy writes for whatever
                is on screen (the usual case). Set "email_reply" only when
                they're clearly replying to an email thread on screen, or
                "cold_dm" for a first-touch message to a person/profile on
                screen.
            length: Optional hint. "short", "medium", or "detailed" only if the
                user actually said so; leave empty to let the field or context
                decide.
            recipient_hint: Who it's for, in the user's words, e.g. "Sarah" or
                "the Clicky team".
            intent: What they want it to say or do, in their words, e.g.
                "say why I'm excited" or "politely decline".
            refine_instruction: ONLY when changing the existing draft: the
                user's change request, e.g. "warmer" or "make it one line".
        """
        # Local @function_tool, so it bypasses ToolExecutor's telemetry span —
        # record it here (the drafter's own LLM calls are traced in ModelProvider).
        span = start_tool_span(
            tool_name="draft_outbound_message", source="voice", uid=self._user_id
        )
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

    @function_tool
    async def present_visible_artifact(
        self,
        kind: str,
        title: str,
        content: str,
        language: str = "",
    ) -> str:
        """Put exact, reusable text in a visible Desktop card.

        Use this for terminal commands, code, configuration, prompts for
        another AI or coding agent, and multi-step guidance. Use it whenever
        reading the full answer aloud would be hard to follow or impossible to
        copy. It is also mandatory when the user corrects you for speaking such
        content. The card is ephemeral, has a copy button, and never executes,
        sends, or persists its content. Never use it for an email reply or DM.
        Never use it as a substitute for a real action: when the user asks to
        create a calendar event, a reminder, or a tracker, call that action
        tool instead of rendering a card or steps about it.

        Args:
            kind: One of command, code, config, prompt, steps, checklist, note.
            title: A short human label for the card, at most a few words.
            content: The complete exact text the user needs. Use Markdown for
                prompts, steps, checklists, and notes. Do not wrap commands,
                code, or config in Markdown fences; the client does that safely.
            language: Optional language or shell label such as powershell,
                bash, python, json, or yaml. Leave empty when it does not apply.
        """
        span = start_tool_span(
            tool_name="present_visible_artifact", source="voice", uid=self._user_id
        )
        try:
            spoken_reply = await _present_visible_artifact(
                user_id=self._user_id,
                session_id=self._session_id,
                kind=kind,
                title=title,
                content=content,
                language=language,
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
        generation_started_at = time.monotonic()
        fresh_frame_available = self._fresh_frame_for_turn
        if (
            self._launch_surface is VoiceSurface.DESKTOP
            and self._screen_frames is not None
        ):
            try:
                fresh_frame_available = (await self._screen_frames.fresh_frame()) is not None
            except Exception:
                fresh_frame_available = False
        latest_user = self._latest_user_message(chat_ctx)
        finalized = bool(
            latest_user is not None
            and latest_user.id == self._finalized_message_id
            and self._finalized_message_id
        )
        transcript = (
            self._finalized_transcript
            if finalized
            else (latest_user.text_content if latest_user is not None else "")
        )
        policy = self._finalized_policy if finalized else None
        if policy is None:
            policy = derive_turn_policy(
                transcript,
                chat_ctx,
                self._launch_surface,
                fresh_frame_available,
                finalized_turn=finalized,
                source_message_id=latest_user.id if latest_user is not None else "",
                turn_index=self._action_telemetry.turn_index,
            )
        inference_tools = [
            tool for tool in tools if tool_name(tool) in policy.allowed_tools
        ]
        exposed_names = [tool_name(tool) for tool in inference_tools]
        inference_ctx = chat_ctx.copy()
        self._action_telemetry.policy(
            policy,
            exposed_names,
            final_stt_message_id=self._finalized_message_id if finalized else "",
        )

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

        raw_stream = Agent.default.llm_node(
            self, inference_ctx, inference_tools, model_settings
        )
        raw_first_chunk_at: float | None = None

        async def _observe_raw_stream(chunks):
            nonlocal raw_first_chunk_at
            async for item in chunks:
                if raw_first_chunk_at is None:
                    raw_first_chunk_at = time.monotonic()
                    logger.info(
                        "VoiceLatency: raw model first chunk",
                        {
                            "session_id": self._session_id,
                            "turn_index": self._action_telemetry.turn_index,
                            "finalized": finalized,
                            "latency_ms": round(
                                (raw_first_chunk_at - generation_started_at) * 1000
                            ),
                        },
                    )
                yield item

        raw_stream = _observe_raw_stream(raw_stream)
        raw_stream = self._apply_execution_safety(
            raw_stream, policy=policy, chat_ctx=chat_ctx
        )
        stream = self._speak_filler_on_tool_calls(raw_stream)
        first_output_logged = False
        async for item in filter_point_tags(stream, on_point=_on_point):
            if not first_output_logged:
                first_output_logged = True
                now = time.monotonic()
                logger.info(
                    "VoiceLatency: llm node first output",
                    {
                        "session_id": self._session_id,
                        "turn_index": self._action_telemetry.turn_index,
                        "finalized": finalized,
                        "node_latency_ms": round((now - generation_started_at) * 1000),
                        "post_model_holdback_ms": (
                            round((now - raw_first_chunk_at) * 1000)
                            if raw_first_chunk_at is not None
                            else None
                        ),
                    },
                )
            self._action_telemetry.first_response()
            yield item

    @staticmethod
    def _latest_user_message(chat_ctx: lk_llm.ChatContext) -> lk_llm.ChatMessage | None:
        for item in reversed(chat_ctx.items):
            if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
                return item
        return None

    async def _apply_execution_safety(self, chunks, *, policy, chat_ctx):
        """Gate complete model-emitted calls before LiveKit's concurrent executor."""
        rejected = False
        had_text = False
        side_effect_emitted = False
        async for item in chunks:
            content = getattr(getattr(item, "delta", None), "content", None)
            had_text = had_text or bool(content) or isinstance(item, str)
            calls = getattr(getattr(item, "delta", None), "tool_calls", None) or []
            if not calls:
                yield item
                continue
            kept = []
            for call in calls:
                registration = VOICE_TOOL_REGISTRY.get(getattr(call, "name", ""))
                decision = evaluate_execution(
                    getattr(call, "name", ""),
                    getattr(call, "arguments", "{}"),
                    policy,
                    chat_ctx,
                )
                if (
                    decision.allowed
                    and registration is not None
                    and registration.effect is not ToolEffect.READ
                ):
                    if side_effect_emitted:
                        decision = type(decision)(False, "side_effect_already_emitted")
                    else:
                        side_effect_emitted = True
                if decision.allowed:
                    kept.append(call)
                    self._action_telemetry.emitted(
                        getattr(call, "name", ""), decision.reason_code
                    )
                else:
                    rejected = True
                    self._action_telemetry.deferred(
                        getattr(call, "name", ""), decision.reason_code
                    )
            item.delta.tool_calls = kept
            yield item
        if rejected and not had_text:
            yield "I couldn't safely run that action. Say what you want me to do once more."

    def record_voice_tool_execution(self, tool_name_value: str, *, success: bool) -> None:
        self._action_telemetry.execution(tool_name_value, success=success)
        self._schedule_context_compaction_check()

    def record_voice_conversation_item(self, item: object) -> None:
        if (
            getattr(item, "role", None) == "assistant"
            and not bool(getattr(item, "interrupted", False))
        ):
            self._schedule_context_compaction_check()

    def _schedule_context_compaction_check(self) -> None:
        async def _check_after_context_update() -> None:
            await asyncio.sleep(0)
            self._context_compactor.maybe_schedule(self.chat_ctx.copy())

        task = asyncio.create_task(
            _check_after_context_update(),
            name=f"voice-compact-check-{self._session_id[:8]}",
        )
        self._context_compaction_checks.add(task)
        task.add_done_callback(self._context_compaction_checks.discard)

    def close_voice_context(self) -> None:
        self._context_compactor.close()
        for task in self._context_compaction_checks:
            task.cancel()

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
