"""The specialized LiveKit agent that owns an armed Guide Mode session.

Guide is a handoff target, not a Buddy prompt mutation. It deliberately gets no
session MCP servers and exposes only native stop plus one bounded planning task.
Durable task work stays in ``GuideTaskRuntime``; ordinary screen questions remain
one direct model call so this boundary does not add steady-state voice latency.
"""

from __future__ import annotations

import asyncio

from livekit.agents import Agent, ModelSettings, RunContext, function_tool
from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from ...prompts import GUIDE_SYSTEM_PROMPT
from .guide_control import SPOKEN_GUIDE_REQUEST_FAILED, request_guide_mode
from .guide_planning_task import GuidePlanningTask
from .guide_session_state import GuidePhase, GuideStartClaim
from .guide_task_runtime import GuideTaskRuntime
from .interview import VoiceSessionState
from .point_tag import PointTarget, filter_point_tags, publish_element_point
from .screen_frames import ScreenFrameStore, attach_screen_frame_to_turn


def _latest_user_message(chat_ctx: lk_llm.ChatContext) -> lk_llm.ChatMessage | None:
    for item in reversed(chat_ctx.items):
        if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
            return item
    return None


def _planning_fillers(dynamic_phrase: str, *, replanning: bool) -> tuple[str, ...]:
    phrase = " ".join(dynamic_phrase.split())
    if not phrase or len(phrase) > 90:
        phrase = (
            "Let me reorient from here."
            if replanning
            else "Let me map that out."
        )
    second = (
        "I'm checking the cleanest way forward."
        if replanning
        else "I'm checking that against your screen."
    )
    return (
        phrase,
        second,
        "Almost there, I'm confirming the next click.",
    )


class GuideSupervisorAgent(Agent):
    """Own Guide conversation turns until the native client disarms the mode."""

    def __init__(
        self,
        *,
        state: VoiceSessionState,
        claim: GuideStartClaim,
        chat_ctx: lk_llm.ChatContext,
        screen_frames: ScreenFrameStore,
        task_runtime: GuideTaskRuntime,
        user_id: str,
        session_id: str,
        display_name: str,
        activation_ready: asyncio.Future[bool],
    ) -> None:
        super().__init__(
            instructions=GUIDE_SYSTEM_PROMPT.format(name=display_name or "there"),
            chat_ctx=chat_ctx,
            # Guide has no access to Buddy's broad MCP surface. Its two local
            # tools are the decorated methods below.
            mcp_servers=None,
        )
        self._state = state
        self._claim = claim
        self._screen_frames = screen_frames
        self._task_runtime = task_runtime
        self._user_id = user_id
        self._session_id = session_id
        self._activation_ready = activation_ready
        self._finalized_message_id = ""
        self._last_frame_id = ""
        self._last_frame_scale = 1.0
        self._current_turn_context_id = ""
        self._proactive_message_id = ""
        self._point_publish_tasks: set[asyncio.Task] = set()

    async def on_enter(self) -> None:
        guide = self._state.guide
        try:
            if not guide.commit_entry(self._claim):
                self._resolve_activation(False)
                return
            self._task_runtime.activate(
                guide_session_id=self._claim.guide_session_id,
                protocol_version=self._claim.protocol_version,
                resume_task_id=self._claim.resume_task_id or None,
            )
            if not guide.note_active():
                await self._task_runtime.deactivate(cancelled=True)
                guide.terminate("activation_state_failed")
                self._resolve_activation(False)
                return
            self._resolve_activation(True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            guide.terminate("supervisor_entry_failed")
            self._resolve_activation(False)
            logger.warn(
                "GuideSupervisor: entry failed",
                {
                    "session_id": self._session_id,
                    "guide_session_id": self._claim.guide_session_id,
                    "error_type": type(exc).__name__,
                },
            )

    async def on_exit(self) -> None:
        guide = self._state.guide
        if guide.phase in {GuidePhase.PLANNING, GuidePhase.ACTIVE}:
            guide.terminate("supervisor_exited")

    def _resolve_activation(self, active: bool) -> None:
        if not self._activation_ready.done():
            self._activation_ready.set_result(active)

    async def on_user_turn_completed(
        self, turn_ctx: lk_llm.ChatContext, new_message: lk_llm.ChatMessage
    ) -> None:
        frame = await attach_screen_frame_to_turn(
            self._screen_frames,
            turn_ctx,
            new_message,
            session_id=self._session_id,
            user_id=self._user_id,
        )
        self._last_frame_id = frame.frame_id if frame else ""
        self._last_frame_scale = frame.model_scale if frame else 1.0
        self._current_turn_context_id = frame.turn_context_id if frame else ""
        if frame is not None:
            self._screen_frames.mark_turn_consumed(frame.turn_context_id)
        self._finalized_message_id = new_message.id
        self._task_runtime.note_activity()

    def prepare_proactive_turn(
        self,
        *,
        message_id: str,
        frame_id: str,
        model_scale: float,
        turn_context_id: str,
    ) -> None:
        """Mark the exact worker-created message that may use the durable runtime."""
        self._proactive_message_id = message_id
        self._last_frame_id = frame_id
        self._last_frame_scale = model_scale
        self._current_turn_context_id = turn_context_id

    async def llm_node(
        self,
        chat_ctx: lk_llm.ChatContext,
        tools: list,
        model_settings: ModelSettings,
    ):
        latest_user = _latest_user_message(chat_ctx)
        proactive = bool(
            latest_user is not None
            and self._proactive_message_id
            and latest_user.id == self._proactive_message_id
        )
        if proactive:
            self._proactive_message_id = ""
            if not self._task_runtime.should_delegate():
                return
            spoken = await self._task_runtime.generate(
                chat_ctx,
                current_turn_context_id=self._current_turn_context_id,
                proactive=True,
            )
            if spoken:
                yield spoken
            return

        finalized = bool(
            latest_user is not None
            and self._finalized_message_id
            and latest_user.id == self._finalized_message_id
        )
        # A normal Guide reply must see the frame attached at finalization. Do
        # not spend a speculative model call on an imageless draft that will be
        # discarded as soon as that frame arrives.
        if not finalized:
            return
        point_published = False

        def _on_point(target: PointTarget) -> None:
            nonlocal point_published
            if point_published or not finalized or not self._last_frame_id:
                return
            point_published = True
            task = asyncio.create_task(
                publish_element_point(
                    target,
                    frame_id=self._last_frame_id,
                    session_id=self._session_id,
                    user_id=self._user_id,
                    coordinate_scale=self._last_frame_scale,
                ),
                name=f"guide-point-{self._session_id[:8]}",
            )
            self._point_publish_tasks.add(task)
            task.add_done_callback(self._point_publish_tasks.discard)

        raw_stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        async for item in filter_point_tags(raw_stream, on_point=_on_point):
            yield item

    @function_tool(on_duplicate="reject")
    async def plan_guide_task(
        self,
        context: RunContext[VoiceSessionState],
        *,
        thinking_phrase: str,
    ) -> None:
        """Plan or replan a multi-step Guide task and return one grounded next action.

        Use this when the user's outcome needs multiple actions, when they answer
        a clarification for the active task, or when the active task needs to be
        reoriented. Do not use it for one visible-control lookup, a simple screen
        question, or a request to stop Guide Mode.

        Args:
            thinking_phrase: A natural three-to-eight-word acknowledgement to say
                only if planning is still running. Do not repeat the request,
                claim the answer is ready, or mention models or internal work.
        """
        guide = context.userdata.guide
        if (
            not guide.is_current(
                self._claim.guide_session_id, self._claim.ownership_epoch
            )
            or guide.phase is not GuidePhase.ACTIVE
        ):
            raise lk_llm.StopResponse()

        replanning = self._task_runtime.task is not None
        fillers = _planning_fillers(thinking_phrase, replanning=replanning)

        def _next_filler(step: int) -> str | None:
            return fillers[step] if step < len(fillers) else None

        task = GuidePlanningTask(
            state=context.userdata,
            runtime=self._task_runtime,
            chat_ctx=self.chat_ctx.copy(exclude_instructions=True),
            current_turn_context_id=self._current_turn_context_id,
        )
        try:
            async with context.with_filler(
                _next_filler,
                delay=0.45,
                interval=3.0,
                max_steps=len(fillers),
            ):
                result = await task
        except Exception as exc:
            logger.warn(
                "GuideSupervisor: planning task failed",
                {
                    "session_id": self._session_id,
                    "guide_session_id": self._claim.guide_session_id,
                    "ownership_epoch": self._claim.ownership_epoch,
                    "error_type": type(exc).__name__,
                },
            )
            if guide.is_current(
                self._claim.guide_session_id, self._claim.ownership_epoch
            ) and guide.phase is GuidePhase.ACTIVE:
                context.session.say(
                    "I couldn't plan that just now. Ask me again in a moment."
                )
            raise lk_llm.StopResponse()

        if result.current and result.spoken_text:
            context.session.say(result.spoken_text)
        raise lk_llm.StopResponse()

    @function_tool
    async def stop_guide_mode(
        self, context: RunContext[VoiceSessionState]
    ) -> str:
        """Stop Guide Mode when the user asks to leave it or return to Buddy."""
        del context
        result = await request_guide_mode(
            user_id=self._user_id,
            voice_session_id=self._session_id,
            enable=False,
        )
        if not result.requested:
            return SPOKEN_GUIDE_REQUEST_FAILED
        return result.spoken
