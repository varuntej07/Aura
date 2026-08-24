"""One bounded typed planning pass inside the Guide supervisor.

The Guide supervisor owns the conversation. This ``AgentTask`` temporarily owns
only the slower planning and visual-decision work, then returns a typed result to
the same supervisor. It has no LLM or tools of its own: the durable runtime calls
Aura's validated structured providers directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from livekit.agents import AgentTask
from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from .guide_session_state import GuidePhase
from .guide_task_runtime import GuideTaskRuntime
from .interview import VoiceSessionState

PLANNING_TASK_INSTRUCTIONS = """
Run one bounded Guide planning operation and return its typed result to the
Guide supervisor. Do not speak, ask questions directly, or call tools.
""".strip()


@dataclass(frozen=True, slots=True)
class GuidePlanningTaskResult:
    """Result stamped to the Guide ownership epoch that requested it."""

    spoken_text: str
    task_id: str
    task_status: str
    guide_session_id: str
    ownership_epoch: int
    current: bool


class GuidePlanningTask(AgentTask[GuidePlanningTaskResult]):
    """Plan or replan once, then give the Guide supervisor its floor back."""

    def __init__(
        self,
        *,
        state: VoiceSessionState,
        runtime: GuideTaskRuntime,
        chat_ctx: lk_llm.ChatContext,
        current_turn_context_id: str,
    ) -> None:
        super().__init__(
            instructions=PLANNING_TASK_INSTRUCTIONS,
            chat_ctx=chat_ctx,
            tools=[],
            llm=None,
            mcp_servers=None,
        )
        self._state = state
        self._runtime = runtime
        self._current_turn_context_id = current_turn_context_id
        self._guide_session_id = state.guide.guide_session_id
        self._ownership_epoch = state.guide.ownership_epoch

    async def on_enter(self) -> None:
        guide = self._state.guide
        if (
            not guide.is_current(self._guide_session_id, self._ownership_epoch)
            or guide.phase is not GuidePhase.ACTIVE
            or not guide.begin_planning()
        ):
            self.complete(self._result("", current=False))
            return

        spoken = ""
        try:
            spoken = await self._runtime.generate(
                self.chat_ctx,
                current_turn_context_id=self._current_turn_context_id,
                force_replan=self._runtime.task is not None,
            )
        except Exception as exc:
            logger.warn(
                "GuidePlanningTask: planning failed",
                {
                    "guide_session_id": self._guide_session_id,
                    "ownership_epoch": self._ownership_epoch,
                    "error_type": type(exc).__name__,
                },
            )
            spoken = "I couldn't plan that just now. Ask me again in a moment."
        finally:
            same_ownership = guide.is_current(
                self._guide_session_id, self._ownership_epoch
            )
            task = self._runtime.task
            if (
                same_ownership
                and guide.phase in {GuidePhase.PLANNING, GuidePhase.ACTIVE}
                and task is not None
            ):
                guide.adopt_task(task.task_id)
            if same_ownership and guide.phase is GuidePhase.PLANNING:
                guide.finish_planning()

        current = bool(
            guide.is_current(self._guide_session_id, self._ownership_epoch)
            and guide.phase is GuidePhase.ACTIVE
        )
        self.complete(self._result(spoken if current else "", current=current))

    async def on_exit(self) -> None:
        guide = self._state.guide
        if (
            guide.is_current(self._guide_session_id, self._ownership_epoch)
            and guide.phase is GuidePhase.PLANNING
        ):
            guide.finish_planning()

    def _result(self, spoken_text: str, *, current: bool) -> GuidePlanningTaskResult:
        task = self._runtime.task
        return GuidePlanningTaskResult(
            spoken_text=spoken_text,
            task_id=task.task_id if task else "",
            task_status=str(task.status) if task else "",
            guide_session_id=self._guide_session_id,
            ownership_epoch=self._ownership_epoch,
            current=current,
        )
