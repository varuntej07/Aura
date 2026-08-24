"""Aura model-pool adapter for the provider-neutral Guide decision port."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger
from ...prompts import (
    GUIDE_DECISION_SYSTEM_PROMPT,
    GUIDE_PLANNER_SYSTEM_PROMPT,
    guide_decision_user_prompt,
    guide_planning_user_prompt,
)
from ...services.model_provider import get_model_provider
from .guide_kernel import (
    GuideDecisionProvider,
    GuideFrameInput,
    GuideTaskProfile,
    task_projection,
)
from .guide_models import GuidePlanningDecision, GuideTask, GuideVisualDecision


class AuraGuideDecisionProvider(GuideDecisionProvider):
    """Typed stateless calls through Aura's existing provider fallback pool."""

    provider_id = "aura_model_pool"

    async def _bounded(
        self,
        *,
        kind: str,
        primary,
        fallback,
        first_timeout: float,
        total_timeout: float,
        primary_model: str,
        fallback_model: str,
        correlation: dict[str, Any],
    ):
        started = time.monotonic()
        provider_fallback = False
        try:
            result = await asyncio.wait_for(primary(), timeout=first_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as first_error:
            provider_fallback = True
            remaining = max(0.05, total_timeout - (time.monotonic() - started))
            logger.warn(
                "GuideTelemetry: provider fallback",
                {
                    **correlation,
                    "kind": kind,
                    "provider_adapter": self.provider_id,
                    "stage": "planning" if kind == "planning" else "verification",
                    "outcome": "retrying",
                    "error_type": type(first_error).__name__,
                    "remaining_ms": round(remaining * 1000),
                },
            )
            try:
                result = await asyncio.wait_for(fallback(), timeout=remaining)
            except asyncio.CancelledError:
                raise
            except Exception as fallback_error:
                logger.warn(
                    "GuideTelemetry: model decision failed",
                    {
                        **correlation,
                        "kind": kind,
                        "provider_adapter": self.provider_id,
                        "stage": "planning" if kind == "planning" else "verification",
                        "outcome": "failed",
                        "reason": "all_provider_attempts_failed",
                        "primary_model": primary_model,
                        "fallback_model": fallback_model,
                        "primary_error_type": type(first_error).__name__,
                        "fallback_error_type": type(fallback_error).__name__,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "deadline_ms": round(total_timeout * 1000),
                    },
                )
                raise
        complete_ms = round((time.monotonic() - started) * 1000)
        logger.info(
            "GuideTelemetry: model decision",
            {
                **correlation,
                "kind": kind,
                "provider_adapter": self.provider_id,
                "first_token_ms": complete_ms,
                "first_token_is_upper_bound": True,
                "complete_ms": complete_ms,
                "provider_fallback": provider_fallback,
            },
        )
        return result

    async def plan(
        self,
        transcript: str,
        *,
        existing_task: GuideTask | None,
        profile: GuideTaskProfile,
        correlation: dict[str, object],
    ) -> GuidePlanningDecision:
        prompt = guide_planning_user_prompt(
            transcript=transcript,
            profile_context=profile.planning_context(),
            task_projection=(
                json.dumps(task_projection(existing_task), separators=(",", ":"))
                if existing_task
                else "none"
            ),
        )
        provider = get_model_provider()
        result = await self._bounded(
            kind="planning",
            primary=lambda: provider.cheap(
                prompt,
                system=GUIDE_PLANNER_SYSTEM_PROMPT,
                response_model=GuidePlanningDecision,
                temperature=0.1,
                max_output_tokens=1800,
            ),
            fallback=lambda: provider.balanced(
                prompt,
                system=GUIDE_PLANNER_SYSTEM_PROMPT,
                response_model=GuidePlanningDecision,
                temperature=0.1,
                max_output_tokens=1800,
            ),
            first_timeout=settings.GUIDE_PLANNING_FIRST_ATTEMPT_TIMEOUT_S,
            total_timeout=settings.GUIDE_PLANNING_DEADLINE_S,
            primary_model=settings.TIER_CHEAP,
            fallback_model=settings.TIER_BALANCED,
            correlation=dict(correlation),
        )
        return GuidePlanningDecision.model_validate(result)

    async def decide(
        self,
        task: GuideTask,
        frame: GuideFrameInput,
        transcript: str,
        *,
        profile: GuideTaskProfile,
        correlation: dict[str, object],
    ) -> GuideVisualDecision:
        prompt = guide_decision_user_prompt(
            task_projection=json.dumps(task_projection(task), separators=(",", ":")),
            transcript=transcript,
            frame_metadata=json.dumps(frame.metadata, separators=(",", ":")),
            profile_context=profile.decision_context(task),
        )
        image = {
            "media_type": "image/jpeg",
            "data": base64.b64encode(frame.image_bytes).decode("ascii"),
        }
        provider = get_model_provider()
        result = await self._bounded(
            kind="visual_decision",
            primary=lambda: provider.balanced(
                prompt,
                system=GUIDE_DECISION_SYSTEM_PROMPT,
                images=[image],
                response_model=GuideVisualDecision,
                temperature=0.0,
            ),
            fallback=lambda: provider.expert(
                prompt,
                system=GUIDE_DECISION_SYSTEM_PROMPT,
                images=[image],
                response_model=GuideVisualDecision,
                temperature=0.0,
            ),
            first_timeout=settings.GUIDE_VISUAL_FIRST_ATTEMPT_TIMEOUT_S,
            total_timeout=settings.GUIDE_VISUAL_DECISION_DEADLINE_S,
            primary_model=settings.TIER_BALANCED,
            fallback_model=settings.TIER_EXPERT,
            correlation=dict(correlation),
        )
        return GuideVisualDecision.model_validate(result)
