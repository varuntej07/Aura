"""Privacy-safe structured telemetry for voice action orchestration."""

from __future__ import annotations

import time

from ...lib.logger import logger
from .action_policy import ACTION_POLICY_VERSION, TurnCapabilityPolicy


class VoiceActionTelemetry:
    def __init__(self, *, session_id: str, surface: str) -> None:
        self._session_id = session_id
        self._surface = surface
        self._turn_index = 0
        self._turn_started_at = time.monotonic()
        self._first_response_logged = False
        self._emitted: list[tuple[str, float]] = []

    @property
    def turn_index(self) -> int:
        return self._turn_index

    def start_turn(self) -> None:
        self.log_interrupted_before_execution()
        self._turn_index += 1
        self._turn_started_at = time.monotonic()
        self._first_response_logged = False

    def policy(
        self,
        policy: TurnCapabilityPolicy,
        exposed: list[str],
        *,
        final_stt_message_id: str,
        unresolved_state_age: int | None,
    ) -> None:
        logger.info(
            "VoiceAction: turn policy",
            {
                "session_id": self._session_id,
                "turn_index": self._turn_index,
                "final_stt_message_id": final_stt_message_id,
                "surface": self._surface,
                "capabilities": sorted(value.value for value in policy.capabilities),
                "unresolved_state_age": unresolved_state_age,
                "missing_slots": list(policy.missing_slots),
                "exposed_tools": sorted(exposed),
                "action_mode": policy.action_mode.value,
                "reason_codes": list(policy.reason_codes),
                "clarification": bool(policy.clarification_question),
                "clarification_owner": policy.clarification_owner,
                "action_policy_version": ACTION_POLICY_VERSION,
            },
        )

    def first_response(self) -> None:
        if self._first_response_logged:
            return
        self._first_response_logged = True
        logger.info(
            "VoiceAction: first response",
            {
                "session_id": self._session_id,
                "turn_index": self._turn_index,
                "surface": self._surface,
                "first_response_latency_ms": round(
                    (time.monotonic() - self._turn_started_at) * 1000
                ),
            },
        )

    def emitted(self, tool_name: str, reason: str) -> None:
        self._emitted.append((tool_name, time.monotonic()))
        logger.info(
            "VoiceAction: tool emitted",
            {
                "session_id": self._session_id,
                "turn_index": self._turn_index,
                "surface": self._surface,
                "tool": tool_name,
                "execution_reason": reason,
            },
        )

    def execution(self, tool_name: str, *, success: bool) -> None:
        started_at = None
        for index, (emitted_name, emitted_at) in enumerate(self._emitted):
            if emitted_name == tool_name:
                started_at = emitted_at
                self._emitted.pop(index)
                break
        logger.info(
            "VoiceAction: tool completed",
            {
                "session_id": self._session_id,
                "turn_index": self._turn_index,
                "surface": self._surface,
                "tool": tool_name,
                "success": success,
                "tool_completion_latency_ms": (
                    round((time.monotonic() - started_at) * 1000)
                    if started_at is not None
                    else None
                ),
            },
        )

    def deferred(self, tool_name: str, reason: str) -> None:
        logger.info(
            "VoiceAction: execution deferred",
            {
                "session_id": self._session_id,
                "turn_index": self._turn_index,
                "surface": self._surface,
                "tool": tool_name,
                "reason": reason,
            },
        )

    def log_interrupted_before_execution(self) -> None:
        for tool_name, _ in self._emitted:
            logger.info(
                "VoiceAction: emitted tool not executed",
                {
                    "session_id": self._session_id,
                    "turn_index": self._turn_index,
                    "surface": self._surface,
                    "tool": tool_name,
                    "interrupted_before_execution": True,
                },
            )
        self._emitted.clear()
