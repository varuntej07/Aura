"""Startup assertion that no surface has quietly lost a core capability.

Every mechanism that narrows Buddy's tool list is defensible on its own: a surface
allowlist, a tier gate, a semantic selector, a per-turn intent policy. The damage comes
from their intersection, which nobody looks at, and which fails silently. A user was told
"I don't have a set_reminder tool exposed to me right now" fifty minutes after the same
account used that tool, and no log, metric, or test noticed. Zero rows and healthy must
never look identical.

So the intersection gets checked out loud, once, at process start, on both the API and
the voice worker. This is a smoke alarm, not a gate: it logs and returns rather than
refusing to boot, because a missing tool degrades one capability while a failed boot
takes down everything.

Not covered here, deliberately: per-turn narrowing that depends on runtime state (voice
BM25 selection, connector availability). Those cannot be evaluated without a live turn.
The core floor in tool_discovery.py is what holds the line there.
"""

from __future__ import annotations

from .tools import CORE_TOOLS


def verify_core_tool_exposure(*, component: str) -> list[str]:
    """Log any surface where a core tool is structurally unreachable.

    Returns the problems found, so a caller can assert on them; an empty list means every
    surface can reach every core tool.
    """
    from ..lib.logger import logger

    problems: list[str] = []

    # Text chat. resolve_chat_surface_allowed_tools returns None for the unrestricted
    # app surface, so None means "everything canonical" rather than "nothing".
    from ..services.tool_executor import resolve_chat_surface_allowed_tools

    canonical = frozenset(CORE_TOOLS)
    for surface in ("app", "desktop"):
        for contract_version in (1, 3):
            allowed = resolve_chat_surface_allowed_tools(
                surface, contract_version=contract_version
            )
            reachable = canonical if allowed is None else frozenset(allowed)
            missing = sorted(canonical - reachable)
            if missing:
                problems.append(
                    f"text:{surface}:v{contract_version} missing {missing}"
                )

    # Voice. Reads the plain capability registry, never the LiveKit runtime, so the API
    # process can run this check without importing the agent stack.
    from ..agent.voice.capabilities import VOICE_TOOL_REGISTRY, VoiceSurface

    for voice_surface in VoiceSurface:
        reachable = frozenset(
            name
            for name, registration in VOICE_TOOL_REGISTRY.items()
            if voice_surface in registration.allowed_surfaces
        )
        # get_upcoming_events needs a connected calendar, so its absence for a given
        # user is a connector state rather than a policy hole. Registry membership is
        # still checked; per-user connector state is not knowable here.
        missing = sorted(canonical - reachable - {"get_upcoming_events"})
        if missing:
            problems.append(f"voice:{voice_surface.value} missing {missing}")

    if problems:
        logger.error(
            "core_tool_exposure_regression",
            {
                "component": component,
                "problems": problems,
                "core_tools": sorted(canonical),
            },
        )
    else:
        logger.info(
            "core_tool_exposure_verified",
            {"component": component, "core_tools": sorted(canonical)},
        )
    return problems
