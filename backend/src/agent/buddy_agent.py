"""
BuddyAgent — the persona that drives the LiveKit voice session.

Most tools are exposed via MCP at /mcp (see backend/src/handlers/mcp.py) and run
over HTTP in the main backend process. Session-bound tools are local
``@function_tool`` methods on this class. For example, ``save_screen_item``
needs the in-memory ``ScreenFrameStore``, while ``report_feedback`` needs the
trusted current finalized transcript and message ID.
LiveKit's ``Agent`` auto-discovers ``@function_tool``-decorated methods on
``self`` (``find_function_tools``), merging them with the MCP-provided tools
into one tool list for the model — no separate registration needed. Lifecycle:

* on_enter -> stay silent. The first finalized user turn starts the
              conversation; the recorder owns the one silence nudge.

Slow-tool filler phrases are spoken from ``llm_node`` below: a tool call
surfacing in the LLM stream is the only pre-execution signal on this stack, so
``ToolFillerSpeaker`` (voice/tool_filler.py) fires there and speaks once the
turn is committed (agent_state == "thinking"), which is exactly while the tool
is executing. Session events cannot do this (see tool_filler.py's docstring).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable
from dataclasses import replace

from livekit import agents
from livekit.agents import Agent, ModelSettings, RunContext, function_tool
from livekit.agents import llm as lk_llm

from ..lib.logger import logger
from ..services.analytics.llm_telemetry import start_tool_span
from ..services.feedback.feedback_capture import capture_feedback
from ..services.feedback.feedback_schema import (
    VOICE_FEEDBACK_TOOL_DEFINITION,
    FeedbackReport,
    voice_feedback_document_id,
)
from ..services.memory.retrieval import (
    VOICE_RETRIEVAL_BUDGET_S,
    render_relevant_memory_block,
    retrieve_relevant_subgraph,
    should_retrieve_for_message,
)
from ..prompts import GUIDE_SYSTEM_PROMPT, voice_system_prompt
from .voice.action_policy import (
    TurnCapabilityPolicy,
    derive_turn_policy,
    evaluate_execution,
)
from .voice.action_telemetry import VoiceActionTelemetry
from .voice.capabilities import VOICE_TOOL_REGISTRY, ToolEffect, VoiceSurface, tool_name
from .voice.context_compaction import VoiceContextCompactor
from .voice.draft_outbound import (
    SPOKEN_DRAFT_READY,
    SPOKEN_REFINE_READY,
    DraftOutboundSession,
    run_draft_tool,
)
from .voice.emotion_tags import convert_audio_cue_stream
from .voice.guide_control import SPOKEN_GUIDE_REQUEST_FAILED, request_guide_mode
from .voice.guide_task_runtime import GuideTaskRuntime
from .voice.point_tag import PointTarget, filter_point_tags, publish_element_point
from .voice.screen_context_stream import (
    StructuredContext,
    StructuredContextStore,
    collapse_stale_contexts,
)
from .voice.screen_frames import ScreenFrameStore, attach_screen_frame_to_turn
from .voice.screen_saves import save_screen_item as _save_screen_item
from .voice.speculation import SpeculationDecision, TurnMutations, decide, is_reusable
from .voice.text_sanitizer import sanitize_text_stream, strip_nonverbal_cue_stream
from .voice.tool_filler import ToolFillerSpeaker
from .voice.tool_result import action_truth_envelope
from .voice.turn_metrics import VoiceTurnMetrics
from .voice.visible_artifacts import (
    SPOKEN_ARTIFACT_READY,
)
from .voice.visible_artifacts import (
    present_visible_artifact as _present_visible_artifact,
)
from .voice_prompt import render_voice_session_context

# A repeated (title, collection_name) tool call inside this window is treated
# as a double-fire (the model re-emitting a call it already made this turn),
# not a second save. Keyed on the raw args the model sent, before collection-
# name dedup resolves them, since that's what would actually repeat.
_DUPLICATE_SAVE_WINDOW_S = 6.0

_DRAFT_OUTBOUND_MESSAGE_TOOL_DEFINITION = {
    "name": "draft_outbound_message",
    "description": (
        "Create or revise copy-ready prose the user will send, post, or submit to "
        "people using their current desktop screen. Use for email replies, DMs, "
        "comments, posts, reviews, bios, and application responses. Do not use for "
        "prompts, code, commands, configuration, calendar events, reminders, "
        "trackers, or ordinary spoken answers. The draft is rendered as a card and "
        "is never sent automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["new", "refine"],
                "description": (
                    "Use new to create a separate draft. Use refine only to modify "
                    "the current draft."
                ),
            }
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
    "strict": True,
}


class BuddyAgent(agents.Agent):
    def __init__(
        self,
        *,
        user_id: str,
        context_vars: dict[str, str],
        chat_ctx: lk_llm.ChatContext,
        screen_frames: ScreenFrameStore | None = None,
        screen_context: StructuredContextStore | None = None,
        session_id: str = "",
        user_tier: str = "free",
        display_name: str = "",
        launch_surface: str = "app",
        bridged: bool = False,
        turn_metrics: VoiceTurnMetrics | None = None,
    ) -> None:
        voice_surface = VoiceSurface(launch_surface)
        # Per-tool selection guidance is NOT assembled here. It lives in each tool's
        # own description (canonical contracts in shared/tools.py for MCP tools, the
        # @function_tool docstrings below for local ones), because OpenAI's GPT-4.1
        # prompting guide, section "1. Agentic Workflows" -> "Tool Calls", says to
        # "exclusively use the tools field to pass tools, rather than manually
        # injecting tool descriptions into your prompt":
        #   https://developers.openai.com/cookbook/examples/gpt4-1_prompting_guide
        # gpt-4.1-mini follows instructions more literally than its predecessors, so a
        # prompt that also explained a tool gave it two sources for one decision.
        # What stays in the prompt is only what a single description cannot express,
        # because a description cannot see its siblings: cross-tool routing and the
        # turn-authority boundary, selected once for this surface.
        # Segments are ordered least-volatile first so the static persona is a stable
        # cache prefix; per-session values render last. Do not reorder.
        instructions = voice_system_prompt(voice_surface.value) + render_voice_session_context(
            context_vars
        )
        super().__init__(
            instructions=instructions,
            chat_ctx=chat_ctx,
        )
        self._user_id = user_id
        self._screen_frames = screen_frames
        self._screen_context = screen_context
        self._session_id = session_id
        self._launch_surface = voice_surface
        # Realtime-bridge mode: the desktop already opened an instant OpenAI Realtime
        # leg and is mid-conversation. This agent joins silently (no greeting) and waits
        # for the bridge handover; the coordinator drives greet()/seed instead of on_enter.
        self._bridged = bridged
        # Guide Mode is a clean state switch: while armed the whole system prompt is
        # swapped to the small GUIDE_SYSTEM_PROMPT and tools to [] (apply_guide_persona),
        # then restored on disarm. Stash the companion instructions now and the tool
        # list lazily (tools resolve only once the agent is active).
        self._guide_active = False
        self._guide_runtime: GuideTaskRuntime | None = None
        self._guide_name = context_vars.get("name") or "there"
        self._companion_instructions = instructions
        self._companion_tools: list | None = None
        # The frame injected into the current turn; element.point events carry
        # its id so the client maps coordinates against the right geometry, and
        # its model_scale so a worker-side downscale is undone before publishing.
        self._last_injected_frame_id = ""
        self._last_injected_frame_scale = 1.0
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
        self._successful_feedback_message_ids: set[str] = set()
        self._finalized_policy: TurnCapabilityPolicy | None = None
        self._fresh_frame_for_turn = False
        self._action_telemetry = VoiceActionTelemetry(
            session_id=session_id, surface=self._launch_surface.value
        )
        self._context_compactor = VoiceContextCompactor(session_id=session_id)
        self._context_compaction_checks: set[asyncio.Task] = set()
        self._turn_metrics = turn_metrics
        # Speculative-reuse bookkeeping. `_speculation_intent` is what this hook
        # decided; `_finalized_pass_ran` is what actually happened, observed in
        # llm_node. They are logged separately on purpose: intending reuse is
        # not evidence of reuse, and the SDK can still invalidate for its own
        # reasons (a transcript that changed between interim and final).
        self._turn_context_id = ""
        self._speculation_intent: SpeculationDecision | None = None
        self._speculation_turn_index = 0
        self._finalized_pass_ran = False
        self._speculative_fresh_frame: bool | None = None
        # Structured UI context injected into the persistent context early
        # enough for the speculation to have snapshotted it. Deferred ids are
        # ones that arrived while a speech was in flight and had to wait for
        # finalization, which is the `context_arrived_late` decision.
        self._context_injection_tasks: set[asyncio.Task] = set()
        self._deferred_context_ids: set[str] = set()
        self._early_context_turn_id = ""
        # Serializes the two paths that can consume a structured snapshot: the
        # background injection task and finalization. Without it a snapshot can
        # be consumed by one and attributed by the other, which lands its id on
        # the WRONG turn (see the claim block in on_user_turn_completed).
        self._context_lock = asyncio.Lock()
        # A speculative pass that emitted a write tool call is recorded against
        # the speculation epoch it belonged to, never as a bare boolean: the
        # speculative stream can outlive its own turn, and a late flag must be
        # incapable of invalidating the NEXT turn's reply.
        self._speculation_epoch = 0
        self._speculative_write_epoch: int | None = None

    def set_guide_frame(self, frame_id: str, model_scale: float = 1.0) -> None:
        """Correlate a Guide Mode [POINT] tag with the frame being discussed."""
        self._last_injected_frame_id = frame_id
        self._last_injected_frame_scale = model_scale
        self._fresh_frame_for_turn = True

    def bind_guide_runtime(self, runtime: GuideTaskRuntime) -> None:
        """Bind the durable Guide path without creating another LiveKit Agent."""
        self._guide_runtime = runtime

    async def apply_guide_persona(self, active: bool) -> None:
        """Swap the whole agent to the guide skill (no tools) on arm, restore on disarm.

        Fail-soft: a failed swap must never break the live session. LiveKit's
        update_instructions/update_tools take effect for every subsequent generation,
        so the guide prompt is active before the next turn even begins.
        """
        try:
            if active:
                if self._companion_tools is None:
                    self._companion_tools = self.tools
                await self.update_instructions(
                    GUIDE_SYSTEM_PROMPT.format(name=self._guide_name)
                )
                await self.update_tools([])
                self._guide_active = True
                logger.info(
                    "VoiceSession: guide persona applied",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        "stage": "execution",
                        "outcome": "succeeded",
                        "reason": "guide_persona_applied",
                    },
                )
            else:
                await self.update_instructions(self._companion_instructions)
                await self.update_tools(self._companion_tools or [])
                self._guide_active = False
                logger.info("VoiceSession: guide persona restored", {
                    "session_id": self._session_id, "user_id": self._user_id,
                })
        except Exception as exc:
            logger.warn(
                "VoiceSession: guide persona swap failed",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "active": active,
                    "error_type": type(exc).__name__,
                    "stage": "execution",
                    "outcome": "failed",
                    "reason": "guide_persona_swap_failed",
                },
            )

    def is_guide_active(self) -> bool:
        return self._guide_active

    async def on_enter(self) -> None:
        # Conversation starts with the user's first finalized turn. A model-picked
        # memory opener is not allowed to manufacture relevance before they speak.
        return

    async def greet(self) -> None:
        # Kept for bridge-handover compatibility. Silence remains server policy
        # until a finalized user turn or the 45-second away event.
        return

    async def on_user_turn_completed(
        self, turn_ctx: lk_llm.ChatContext, new_message: lk_llm.ChatMessage
    ) -> None:
        """Finalize turn state, mutating the context ONLY where something is new.

        Every mutation here throws away the reply LiveKit already speculatively
        generated (it reuses that reply only when the transcript, chat context
        and tool list are all untouched afterwards), so this hook mutates
        deliberately and names exactly what it did. An ordinary conversational
        turn -- no fresh screen frame, no new structured UI context, no memory
        subgraph, no compaction -- mutates nothing and keeps the speculation.
        That is the entire point of the vocabulary in voice/speculation.py.

        Side-effect safety does NOT depend on invalidating here, but be precise
        about what does carry it, because the exposure filter no longer does.
        llm_node deliberately derives the EXPOSURE policy with finalized_turn=True
        on both passes so the model sees identical tool schemas (a speculation
        generated against a smaller tool set is not a safe substitute for the
        finalized reply). derive_turn_policy therefore cannot tell the passes
        apart any more. Authorization moved to the separate EXECUTION policy,
        which carries the real finalized flag: evaluate_execution refuses every
        non-READ call a speculative pass emits, and that refusal sets
        _speculative_write_attempt, which invalidates this turn so the finalized
        pass performs the action properly. LiveKit's scheduling boundary (tools
        run only after the speech handle is scheduled) is a third layer, not the
        only one.

        Never raises: a raised hook drops the user's whole reply.
        """
        turn_finalize_started = time.monotonic()
        self._resolve_previous_speculation()
        self._turn_context_id = ""

        compacted_context = self._context_compactor.apply_ready(turn_ctx)
        if compacted_context is None:
            compacted_context = self._context_compactor.enforce_hard_ceiling(turn_ctx)
        context_was_compacted = compacted_context is not None
        if compacted_context is not None:
            turn_ctx.items[:] = compacted_context.items
        finalized_transcript = new_message.text_content
        # Guide Mode answers only from the current screen; pulling memory here would
        # invite the very restating/parroting Guide Mode must avoid, and costs latency.
        graph_context_appended = False
        if not self._guide_active:
            graph_context_appended = await self._append_live_graph_context(
                turn_ctx, finalized_transcript
            )

        # Structured UI context that never made it into the persistent context
        # while the user was still speaking. Appending it here is correct but
        # costs the speculation, so the two cases are logged apart.
        structured_appended = False
        structured_late = False
        # Claiming the early-injected id and consuming any still-unconsumed
        # snapshot happen together under one lock. Split apart, the injection
        # task can slot in between them: it consumes the snapshot and sets the
        # early id AFTER this turn already claimed "", so this turn reports no
        # context and the NEXT turn inherits an id for a screen it never saw.
        async with self._context_lock:
            claimed_early_context_id = self._early_context_turn_id
            self._early_context_turn_id = ""
            structured_context = self._unconsumed_structured_context()
            if structured_context is not None and self._screen_context is not None:
                # Same one-hot-block rule as the early-injection path.
                collapse_stale_contexts(turn_ctx)
                turn_ctx.add_message(role="system", content=[structured_context.rendered])
                self._screen_context.mark_consumed(structured_context.turn_context_id)
                structured_appended = True
                structured_late = (
                    structured_context.turn_context_id in self._deferred_context_ids
                )

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
            self._last_injected_frame_scale = frame.model_scale if frame else 1.0
            if frame is not None:
                self._screen_frames.mark_turn_consumed(frame.turn_context_id)
        self._action_telemetry.start_turn()
        self._fresh_frame_for_turn = frame is not None
        self._finalized_message_id = new_message.id
        self._finalized_transcript = finalized_transcript
        current_turn_index = self._action_telemetry.turn_index
        if self._turn_metrics is not None:
            self._turn_metrics.start_turn(
                turn_index=current_turn_index,
                user_transcript=finalized_transcript,
                frame_id=frame.frame_id if frame is not None else None,
            )
        policy = derive_turn_policy(
            finalized_transcript,
            turn_ctx,
            self._launch_surface,
            self._fresh_frame_for_turn,
            source_message_id=new_message.id,
            turn_index=current_turn_index,
        )
        self._finalized_policy = policy
        # The speculative llm_node pass derived its own policy from the frame
        # availability it saw. If that flipped, the two passes had different
        # tool sets and reusing the earlier one would answer with the wrong
        # capabilities, so it is named as its own invalidation reason.
        tool_policy_changed = (
            self._speculative_fresh_frame is not None
            and self._speculative_fresh_frame != self._fresh_frame_for_turn
        )

        # Derived from what this turn actually carried, in order of specificity.
        # A turn with none of these keeps "", which is what makes a context-free
        # turn distinguishable in the metrics from one that saw the screen.
        if structured_context is not None:
            self._turn_context_id = structured_context.turn_context_id
        elif frame is not None and frame.turn_context_id:
            self._turn_context_id = frame.turn_context_id
        else:
            self._turn_context_id = claimed_early_context_id

        # A speculative pass tried to act and was refused by the execution gate.
        # Naming the reason is not enough on its own: if nothing else mutated,
        # LiveKit would happily reuse that speculation and the user would hear
        # its refusal line. This append is what actually forces the cold pass,
        # which then runs with finalized authorization and performs the action.
        # Only a flag raised by THIS turn's speculation counts. A speculative
        # stream can still be running when its turn finalizes, so a bare boolean
        # would let a late write attempt land on the next turn and invalidate a
        # reply that never tried to act. Comparing epochs makes a stale flag
        # inert: it can only ever match the epoch it was raised under.
        speculative_write_attempt = (
            self._speculative_write_epoch is not None
            and self._speculative_write_epoch == self._speculation_epoch
        )
        self._speculative_write_epoch = None
        self._speculation_epoch += 1
        if speculative_write_attempt:
            turn_ctx.add_message(
                role="system",
                content=["<speculative_action_retry>finalized turn</speculative_action_retry>"],
            )

        mutations = TurnMutations(
            guide_active=self._guide_active,
            context_compacted=context_was_compacted,
            graph_context_appended=graph_context_appended,
            structured_context_appended=structured_appended and not structured_late,
            structured_context_arrived_late=structured_late,
            screen_frame_attached=frame is not None,
            tool_policy_changed=tool_policy_changed,
            speculative_write_attempt=speculative_write_attempt,
        )
        decision = decide(mutations)
        self._speculation_intent = decision
        self._speculation_turn_index = current_turn_index
        self._finalized_pass_ran = False
        self._speculative_fresh_frame = None
        turn_finalize_ms = round((time.monotonic() - turn_finalize_started) * 1000)
        if self._turn_metrics is not None:
            strategy = "none"
            if structured_appended and frame is not None:
                strategy = "both"
            elif frame is not None:
                strategy = "pixels"
            elif structured_appended or self._turn_context_id:
                strategy = "structured"
            self._turn_metrics.note_turn_context(
                turn_index=current_turn_index,
                turn_context_id=self._turn_context_id,
                context_strategy=strategy,
                structured_context_bytes=(
                    structured_context.raw_bytes if structured_context is not None else 0
                ),
                stream_assembly_ms=(
                    structured_context.assembly_ms if structured_context is not None else None
                ),
                schema_validation_ms=(
                    structured_context.validation_ms if structured_context is not None else None
                ),
            )
            self._turn_metrics.note_speculation(
                turn_index=current_turn_index,
                decision_reason=decision.value,
                turn_finalize_ms=turn_finalize_ms,
            )
        logger.info(
            "VoiceLatency: preemptive generation decision",
            {
                "session_id": self._session_id,
                "turn_index": current_turn_index,
                "turn_context_id": self._turn_context_id,
                "decision": decision.value,
                "reusable": is_reusable(decision),
                "mutations": list(mutations.applied()),
                "exposed_tool_count": len(policy.allowed_tools),
                "turn_finalize_ms": turn_finalize_ms,
            },
        )
        if context_was_compacted:
            await self.update_chat_ctx(turn_ctx)

    def _unconsumed_structured_context(self) -> StructuredContext | None:
        """The newest structured snapshot no turn has attached yet, if any."""
        if self._screen_context is None:
            return None
        try:
            return self._screen_context.fresh_context()
        except Exception:
            return None

    def _resolve_previous_speculation(self) -> None:
        """Report what actually happened to the previous turn's speculation.

        Intent is not evidence. LiveKit can still discard a speculation this
        hook left untouched, most often because the interim transcript it was
        generated from did not match the final one. The only reliable local
        signal is whether a finalized llm_node pass ran: if it did, the cold
        path was taken. This resolves one turn late, which is fine for a metric.
        """
        intent = self._speculation_intent
        if intent is None:
            return
        self._speculation_intent = None
        observed_reuse = not self._finalized_pass_ran
        if self._turn_metrics is not None:
            self._turn_metrics.note_speculative_outcome(
                turn_index=self._speculation_turn_index,
                reused=observed_reuse,
            )
        logger.info(
            "VoiceLatency: speculative outcome",
            {
                "session_id": self._session_id,
                "turn_index": self._speculation_turn_index,
                "intended_reuse": is_reusable(intent),
                "observed_reuse": observed_reuse,
                "decision": intent.value,
            },
        )

    def ingest_structured_context(self, context: StructuredContext) -> None:
        """Sync listener for the ``screen_context`` store; injects off the callback.

        Injecting the snapshot into the PERSISTENT context while the user is
        still speaking is what lets a screen-aware turn keep its speculative
        reply: the speculation snapshots a context that already contains it, so
        on_user_turn_completed has nothing left to append.
        """
        task = asyncio.create_task(
            self._inject_structured_context(context),
            name=f"voice-context-inject-{self._session_id[:8]}",
        )
        self._context_injection_tasks.add(task)
        task.add_done_callback(self._context_injection_tasks.discard)

    async def _inject_structured_context(self, context: StructuredContext) -> None:
        """Fail-soft early injection. Defers rather than racing a live speech."""
        if self._screen_context is None or not context.rendered:
            return
        try:
            if self._guide_active or self._speech_in_flight():
                self._deferred_context_ids.add(context.turn_context_id)
                return
            async with self._context_lock:
                # Recheck under the lock. Between this task being scheduled and
                # reaching here, a finalized turn may already have claimed and
                # consumed this exact snapshot. Injecting it again would attach
                # the same screen twice and, worse, leave an early id that the
                # NEXT turn would claim for a screen it never saw.
                current = self._screen_context.fresh_context()
                if current is None or current.turn_context_id != context.turn_context_id:
                    return
                chat_ctx = self.chat_ctx.copy()
                # Exactly one snapshot stays hot. Without this every early
                # injection stacks another block onto the persistent context and
                # the model reads several contradictory screens at once.
                collapse_stale_contexts(chat_ctx)
                chat_ctx.add_message(role="system", content=[context.rendered])
                await self.update_chat_ctx(chat_ctx)
                self._screen_context.mark_consumed(context.turn_context_id)
                # Held, not published: the id belongs to whichever finalized turn
                # claims it, so a later context-free turn cannot inherit it.
                self._early_context_turn_id = context.turn_context_id
            logger.info(
                "VoiceLatency: screen context injected early",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "turn_context_id": context.turn_context_id,
                    "structured_context_bytes": context.raw_bytes,
                    "stream_assembly_ms": context.assembly_ms,
                    "schema_validation_ms": context.validation_ms,
                },
            )
        except Exception as exc:
            # An agent that is not active yet, or a context update that lost a
            # race, both land here. The snapshot stays unconsumed, so
            # finalization picks it up as `context_arrived_late`.
            self._deferred_context_ids.add(context.turn_context_id)
            logger.warn(
                "VoiceSession: early screen context injection deferred",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "error_type": type(exc).__name__,
                    "outcome": "failed",
                    "reason": "early_injection_deferred",
                },
            )

    def _speech_in_flight(self) -> bool:
        """True while Buddy is generating or speaking, when the context must not move."""
        try:
            state = str(getattr(self.session, "agent_state", ""))
        except Exception:
            return True
        return state in {"thinking", "speaking"}

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
    ) -> dict[str, object]:
        """Save the thing on screen the user just asked Buddy to remember.

        Call this ONLY when the user explicitly asks to save/remember/bookmark
        something visible on their screen right now ("save these shoes",
        "remember this recipe", "keep this for later") — never speculatively,
        and never for something with no visual referent (use a reminder or
        memory tool instead for those). Never use it to put text on screen for
        the user to read or copy; present_visible_artifact owns that. Persists
        the current screen-sight frame plus what you saw, so the user can
        revisit it later from their dashboard.

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
            return action_truth_envelope(
                ok=True,
                say="Already saved that.",
                render_mode="verbatim",
                render_channel="voice",
            )

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
        self._recent_screen_saves[dedup_key] = now
        span.finish()
        return action_truth_envelope(
            ok=result.item_id is not None,
            say=result.spoken_confirmation,
            render_mode="verbatim",
            render_channel="voice",
        )

    @function_tool(raw_schema=VOICE_FEEDBACK_TOOL_DEFINITION)
    async def report_feedback(
        self,
        raw_arguments: dict[str, object],
    ) -> dict[str, object]:
        """Persist feedback using only trusted finalized-turn context."""
        if not isinstance(raw_arguments, dict):
            raise lk_llm.ToolError("report_feedback input must be an object.")

        schema = VOICE_FEEDBACK_TOOL_DEFINITION["parameters"]
        properties = schema["properties"]
        required_fields = schema["required"]
        missing_fields = [
            field for field in required_fields if field not in raw_arguments
        ]
        if missing_fields:
            raise lk_llm.ToolError(
                f"report_feedback is missing required field: {missing_fields[0]}."
            )
        unknown_fields = sorted(set(raw_arguments) - set(properties))
        if unknown_fields:
            raise lk_llm.ToolError(
                f"report_feedback received unknown field: {unknown_fields[0]}."
            )

        for field in required_fields:
            value = raw_arguments[field]
            if not isinstance(value, str):
                raise lk_llm.ToolError(f"report_feedback field {field} must be a string.")
            allowed_values = properties[field].get("enum")
            if allowed_values is not None and value not in allowed_values:
                raise lk_llm.ToolError(
                    f"report_feedback field {field} has an invalid value."
                )

        finalized_transcript = self._finalized_transcript
        finalized_message_id = self._finalized_message_id
        if not finalized_transcript.strip():
            raise lk_llm.ToolError(
                "report_feedback requires a non-empty finalized transcript."
            )
        if not finalized_message_id:
            raise lk_llm.ToolError(
                "report_feedback requires a finalized message ID."
            )

        # The envelope, not the tool description, owns post-call speech (see
        # test_tool_descriptions_contain_selection_and_argument_guidance_only).
        # Spelled out because the short version lost: a real session produced
        # "Got it, I reported that the voice agent feels robotic", which turns a
        # user's frustration into a ticket receipt instead of a conversation.
        continuation = (
            "Say nothing about feedback, reporting, logging, filing, noting, or "
            "passing anything along, and nothing about this tool call or whether "
            "it worked. Reply to what they actually raised as if the call never "
            "happened. Asking you to report something does not change this: they "
            "still want you to engage with the problem itself."
        )
        if finalized_message_id in self._successful_feedback_message_ids:
            return action_truth_envelope(ok=True, then=continuation)

        report = FeedbackReport(
            category=raw_arguments["category"],
            about=raw_arguments["about"],
            summary=raw_arguments["summary"],
            severity=raw_arguments["severity"],
            verbatim_quote=finalized_transcript,
        )
        document_id = voice_feedback_document_id(
            uid=self._user_id,
            session_id=self._session_id,
            finalized_message_id=finalized_message_id,
        )
        span = start_tool_span(
            tool_name="report_feedback",
            source="voice",
            uid=self._user_id,
        )
        captured = await capture_feedback(
            self._user_id,
            report,
            source="voice",
            session_id=self._session_id,
            document_id=document_id,
        )
        span.finish(
            success=captured,
            error_type=None if captured else "durable_capture_failed",
        )
        if captured:
            self._successful_feedback_message_ids.add(finalized_message_id)
        return action_truth_envelope(ok=captured, then=continuation)

    @function_tool(raw_schema=_DRAFT_OUTBOUND_MESSAGE_TOOL_DEFINITION)
    async def draft_outbound_message(
        self,
        ctx: RunContext,
        raw_arguments: dict[str, object],
    ) -> dict[str, object]:
        unknown_fields = set(raw_arguments) - {"operation"}
        operation = raw_arguments.get("operation")
        if (
            unknown_fields
            or not isinstance(operation, str)
            or operation not in {"new", "refine"}
        ):
            raise lk_llm.ToolError(
                "draft_outbound_message requires exactly one field, operation, "
                "with value new or refine."
            )
        # Local @function_tool, so it bypasses ToolExecutor's telemetry span —
        # record it here (the drafter's own LLM calls are traced in ModelProvider).
        span = start_tool_span(
            tool_name="draft_outbound_message", source="voice", uid=self._user_id
        )
        try:
            spoken_reply = await run_draft_tool(
                self._draft_outbound,
                self._screen_frames,
                operation=operation,
                transcript=self._finalized_transcript,
                run_ctx=ctx,
            )
        except Exception as exc:
            span.finish(success=False, error_type=type(exc).__name__)
            raise
        span.finish()
        succeeded = spoken_reply in {SPOKEN_DRAFT_READY, SPOKEN_REFINE_READY}
        return action_truth_envelope(
            ok=succeeded,
            say=spoken_reply,
            render_mode="verbatim",
            render_channel="card" if succeeded else "voice",
            then=(
                "The card is already rendered. Speak only `say` and emit nothing else; "
                "never recite, preview, or summarize the draft."
                if succeeded
                else "Speak only `say` and do not imply a card was created."
            ),
        )

    @function_tool
    async def present_visible_artifact(
        self,
        ctx: RunContext,
        kind: str,
        title: str,
        content: str,
        language: str = "",
    ) -> dict[str, object]:
        """Put exact, reusable text in a visible Desktop card.

        Use this for terminal commands, code, configuration, prompts or scripts
        for another AI, a coding agent, or a video/UGC generator, and multi-step
        guidance. Use it whenever
        reading the full answer aloud would be hard to follow or impossible to
        copy. It is also mandatory when the user corrects you for speaking such
        content. The card is ephemeral, has a copy button, and never executes,
        sends, or persists its content. Never use it for an email reply or DM.
        Never use it as a substitute for a real action: when the user asks to
        create a calendar event, a reminder, or a tracker, call that action
        tool instead of rendering a card or steps about it. A single simple
        action or a conversational explanation stays spoken; do not reach for a
        card when talking is enough.

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
        succeeded = spoken_reply == SPOKEN_ARTIFACT_READY
        return action_truth_envelope(
            ok=succeeded,
            say=spoken_reply,
            render_mode="verbatim",
            render_channel="card" if succeeded else "voice",
            then=(
                "The card is already rendered. Speak only `say` and emit nothing else; "
                "never recite, preview, or summarize the artifact."
                if succeeded
                else "Speak only `say` and do not imply a card is visible."
            ),
        )

    @function_tool
    async def set_guide_mode(self, enable: bool) -> dict[str, object]:
        """Turn Guide Mode on or off when the user asks for it.

        Guide Mode is the hands-free screen-guidance mode where Buddy watches
        the user's screen and gives one short nudge as it changes. This is the
        ONLY way you can control it. Call set_guide_mode(enable=true) to start
        it and set_guide_mode(enable=false) to stop it, whenever the user asks
        ("start guide mode", "turn on guide mode", "stop guiding me").

        Because this is the only control you have, NEVER say you turned Guide Mode
        on or off, flipped a switch, or that it is now running, unless you called
        this tool on this turn and it returned success. Arming is owned natively by
        the desktop, so a call only REQUESTS the change and can fail after you ask.

        Args:
            enable: True to start Guide Mode, False to stop it.
        """
        span = start_tool_span(tool_name="set_guide_mode", source="voice", uid=self._user_id)
        try:
            spoken_reply = await request_guide_mode(
                user_id=self._user_id, session_id=self._session_id, enable=enable
            )
        except Exception as exc:
            span.finish(success=False, error_type=type(exc).__name__)
            raise
        span.finish()
        return action_truth_envelope(
            ok=spoken_reply != SPOKEN_GUIDE_REQUEST_FAILED,
            say=spoken_reply,
            render_mode="verbatim",
            render_channel="voice",
            then=(
                "Speak only `say`. This confirms a request to the desktop, not that "
                "Guide Mode is already active."
            ),
        )

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
        if self._guide_active:
            if self._guide_runtime is not None and self._guide_runtime.should_delegate():
                logger.info(
                    "GuideTrace",
                    {
                        "session_id": self._session_id,
                        "user_id": self._user_id,
                        **self._guide_runtime.diagnostic_state(),
                        "stage": "execution",
                        "outcome": "started",
                        "reason": "guide_runtime_delegated",
                    },
                )
                spoken = await self._guide_runtime.generate(chat_ctx)
                if spoken:
                    yield spoken
                return
            logger.warn(
                "GuideTrace",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    **(
                        self._guide_runtime.diagnostic_state()
                        if self._guide_runtime is not None
                        else {"runtime_active": False}
                    ),
                    "stage": "execution",
                    "outcome": "rejected",
                    "reason": "guide_runtime_not_delegated",
                },
            )

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
        if finalized:
            # The cold path ran, so whatever was speculated was discarded.
            # _resolve_previous_speculation reads this on the next turn.
            self._finalized_pass_ran = True
        else:
            self._speculative_fresh_frame = fresh_frame_available
        policy = self._finalized_policy if finalized else None
        if policy is None:
            # EXPOSURE policy. finalized_turn=True on both passes deliberately,
            # because the tool SCHEMAS the model sees must be identical for a
            # speculation to be reusable at all - and because a reused
            # speculation generated against a read-only tool set would silently
            # answer a side-effecting request without acting.
            policy = derive_turn_policy(
                transcript,
                chat_ctx,
                self._launch_surface,
                fresh_frame_available,
                finalized_turn=True,
                source_message_id=latest_user.id if latest_user is not None else "",
                turn_index=self._action_telemetry.turn_index,
            )
        # EXECUTION policy, kept separate on purpose. Identical exposure means
        # action_policy's finalized_turn check can no longer distinguish the two
        # passes, so authorization is carried here instead: a speculative pass
        # gets finalized_turn=False and evaluate_execution refuses every non-READ
        # call it emits. That restores a real gate that does not depend on
        # LiveKit's scheduling behaviour, while leaving the model's view of the
        # tools byte-identical between passes.
        execution_policy = replace(policy, finalized_turn=finalized)
        # Captured once, at the start of this generation, and carried with it.
        # If this stream outlives its turn, the epoch it reports will no longer
        # be current and any write attempt it records is discarded rather than
        # charged to whatever turn happens to be finalizing.
        speculation_epoch = self._speculation_epoch
        inference_tools = [
            tool for tool in tools if tool_name(tool) in policy.allowed_tools
        ]
        exposed_names = [tool_name(tool) for tool in inference_tools]
        inference_ctx = chat_ctx.copy()
        current_turn_index = self._action_telemetry.turn_index
        if self._turn_metrics is not None:
            frame_count_in_ctx = sum(
                isinstance(content, lk_llm.ImageContent)
                for item in inference_ctx.items
                if isinstance(item, lk_llm.ChatMessage)
                for content in item.content
            )
            self._turn_metrics.note_model_request(
                turn_index=current_turn_index,
                frame_count_in_ctx=frame_count_in_ctx,
            )
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
            if not finalized:
                # Never move the user's overlay for a reply that may be thrown
                # away. Pointing requires a fresh desktop frame, and attaching a
                # frame always invalidates the speculation, so the finalized
                # pass always runs for a pointing turn and publishes there.
                return
            task = asyncio.create_task(
                publish_element_point(
                    target,
                    frame_id=self._last_injected_frame_id,
                    session_id=self._session_id,
                    user_id=self._user_id,
                    coordinate_scale=self._last_injected_frame_scale,
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
                    raw_latency_ms = round(
                        (raw_first_chunk_at - generation_started_at) * 1000
                    )
                    logger.info(
                        "VoiceLatency: raw model first chunk",
                        {
                            "session_id": self._session_id,
                            "turn_index": self._action_telemetry.turn_index,
                            "finalized": finalized,
                            "latency_ms": raw_latency_ms,
                        },
                    )
                    if self._turn_metrics is not None:
                        self._turn_metrics.note_model_first_chunk(
                            turn_index=current_turn_index,
                            latency_ms=raw_latency_ms,
                        )
                yield item

        raw_stream = _observe_raw_stream(raw_stream)
        raw_stream = self._apply_execution_safety(
            raw_stream,
            policy=execution_policy,
            chat_ctx=chat_ctx,
            speculation_epoch=speculation_epoch,
        )
        stream = self._speak_filler_on_tool_calls(raw_stream)
        first_output_logged = False
        async for item in filter_point_tags(stream, on_point=_on_point):
            if not first_output_logged:
                first_output_logged = True
                now = time.monotonic()
                node_latency_ms = round((now - generation_started_at) * 1000)
                logger.info(
                    "VoiceLatency: llm node first output",
                    {
                        "session_id": self._session_id,
                        "turn_index": self._action_telemetry.turn_index,
                        "finalized": finalized,
                        "node_latency_ms": node_latency_ms,
                        "post_model_holdback_ms": (
                            round((now - raw_first_chunk_at) * 1000)
                            if raw_first_chunk_at is not None
                            else None
                        ),
                    },
                )
                if self._turn_metrics is not None:
                    self._turn_metrics.note_llm_node_first_output(
                        turn_index=current_turn_index,
                        latency_ms=node_latency_ms,
                    )
            self._action_telemetry.first_response()
            yield item

    @staticmethod
    def _latest_user_message(chat_ctx: lk_llm.ChatContext) -> lk_llm.ChatMessage | None:
        for item in reversed(chat_ctx.items):
            if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
                return item
        return None

    async def _apply_execution_safety(
        self, chunks, *, policy, chat_ctx, speculation_epoch=None
    ):
        """Gate complete model-emitted calls before LiveKit's concurrent executor."""
        had_text = False
        side_effect_emitted = False
        buffered_tool_chunks = []
        evaluated_calls = []
        async for item in chunks:
            content = getattr(getattr(item, "delta", None), "content", None)
            had_text = had_text or bool(content) or isinstance(item, str)
            calls = getattr(getattr(item, "delta", None), "tool_calls", None) or []
            if not calls:
                yield item
                continue
            buffered_tool_chunks.append(item)
            for call in calls:
                registration = VOICE_TOOL_REGISTRY.get(getattr(call, "name", ""))
                decision = evaluate_execution(
                    getattr(call, "name", ""),
                    getattr(call, "arguments", "{}"),
                    policy,
                    chat_ctx,
                )
                if (
                    decision.reason_code == "stale_turn_side_effect"
                    and speculation_epoch is not None
                ):
                    # A speculative pass tried to act. Blocked here, and recorded
                    # against the epoch it belonged to so finalization can tell a
                    # flag raised by THIS turn from one raised by a stream that
                    # outlived its own. Only the former invalidates; the latter is
                    # visible in the log and inert.
                    self._speculative_write_epoch = speculation_epoch
                    logger.info(
                        "VoiceLatency: speculative write blocked",
                        {
                            "session_id": self._session_id,
                            "tool": getattr(call, "name", ""),
                            "speculation_epoch": speculation_epoch,
                            "current_epoch": self._speculation_epoch,
                            "stale": speculation_epoch != self._speculation_epoch,
                        },
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
                evaluated_calls.append((call, registration, decision, item))

        surviving = [
            entry
            for entry in evaluated_calls
            if entry[2].allowed and entry[1] is not None
        ]
        if len(surviving) > 1 and not all(
            entry[1].safe_concurrently for entry in surviving
        ):
            concurrency_deferred = {id(entry[0]) for entry in surviving[1:]}
            surviving = surviving[:1]
        else:
            concurrency_deferred = set()

        surviving_ids = {id(entry[0]) for entry in surviving}
        for call, _registration, decision, _item in evaluated_calls:
            name = getattr(call, "name", "")
            if id(call) in surviving_ids:
                self._action_telemetry.emitted(name, decision.reason_code)
            elif id(call) in concurrency_deferred:
                self._action_telemetry.deferred(
                    name, "unsafe_parallel_tool_batch"
                )
            else:
                self._action_telemetry.deferred(name, decision.reason_code)

        if surviving:
            calls_to_emit = [entry[0] for entry in surviving]
            carrier = surviving[0][3]
            for item in buffered_tool_chunks:
                item.delta.tool_calls = (
                    calls_to_emit if item is carrier else []
                )
                if (
                    item.delta.tool_calls
                    or getattr(item.delta, "content", None)
                    or getattr(item.delta, "extra", None)
                    or getattr(item, "usage", None)
                ):
                    yield item
        else:
            for item in buffered_tool_chunks:
                item.delta.tool_calls = []
                if (
                    getattr(item.delta, "content", None)
                    or getattr(item.delta, "extra", None)
                    or getattr(item, "usage", None)
                ):
                    yield item
        if not surviving and evaluated_calls and not had_text:
            # Last-resort speech when every emitted call was gated and the model
            # produced no text of its own, so the alternative is dead air. Kept
            # short, human, and free of any hint at the machinery: the user asked
            # for something, not for a status report on the action policy. The
            # previous wording ("I couldn't safely run that action") read as a
            # refusal and landed mid-conversation as Buddy going robotic.
            yield "Hmm, that didn't go through. Say it once more?"

    def record_voice_tool_execution(
        self, tool_name_value: str, *, success: bool
    ) -> int | None:
        latency_ms = self._action_telemetry.execution(tool_name_value, success=success)
        self._schedule_context_compaction_check()
        return latency_ms

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
