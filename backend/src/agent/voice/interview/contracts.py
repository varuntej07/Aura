"""Wire constants and boundary types for Interview Mode.

Types and literals only. This module imports nothing else in the package so both
sides of a handoff, and the worker wiring, can depend on the contract without
depending on each other.

The desktop half of this contract is documented in ``ECOSYSTEM.md``. Changing any
constant here changes a cross-repo contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from livekit.agents import Agent
from livekit.agents import llm as lk_llm

if TYPE_CHECKING:
    from .models import InterviewDossier

# Returns the agent to hand control back to, given the conversation the departing
# agent accumulated. A factory rather than a plain reference because resuming is
# not free: Buddy has to re-seed its own context and arm its post-interview
# greeting before it is handed the floor. Wired in voice_agent.py, the only place
# that owns the live BuddyAgent instance.
#
# Typed as Agent, not BuddyAgent, on purpose: this module must not import the
# agent it hands back to, or the boundary stops being a boundary.
BuddyFactory = Callable[[lk_llm.ChatContext], Awaitable[Agent]]

MaterialType = Literal["job_description"]

# Byte-stream topic the desktop publishes interview materials on. A separate
# topic from screen_frame/screen_context because it is a different contract with
# a different shape and different bounds; byte-stream topics are one namespace
# and silently sharing one would make two payloads indistinguishable on arrival.
INTERVIEW_MATERIAL_TOPIC = "interview_material"

# Stream attribute names. Read off reader.info.attributes, the same way
# screen_frames.py reads its frame metadata.
ATTR_INTERVIEW_ID = "interview_id"
ATTR_REVISION = "revision"
ATTR_MATERIAL_TYPE = "material_type"
ATTR_SCHEMA_VERSION = "schema_version"

# Wire schema versions this worker understands. A client sending anything else is
# rejected loudly rather than parsed on a guess.
MATERIAL_SCHEMA_VERSION = 1
SUPPORTED_MATERIAL_SCHEMA_VERSIONS = frozenset({MATERIAL_SCHEMA_VERSION})

# Data-channel message types on the reliable client_events topic.
MATERIAL_REQUEST_TYPE = "interview.material.request"
MATERIAL_OVERLAY_SHOWN_TYPE = "interview.material.overlay_shown"

# A job description is prose someone pasted. Well past any real posting, and far
# enough under the data-channel and context limits that an oversize stream is a
# bug or abuse rather than an unlucky long posting. Enforced on receipt
# regardless of what the client claims to have applied.
MAX_MATERIAL_BYTES = 64_000

# One round trip over a reliable data channel is tens of milliseconds. This is
# the outer bound before we stop waiting for proof the paste box is on screen,
# not a budget we expect to spend.
OVERLAY_ACK_TIMEOUT_S = 0.9

# A human has to find the posting, select it, and paste. This bounds that wait so
# a user who wandered off cannot pin the intake open; on expiry the intake falls
# back to asking conversationally.
MATERIAL_ARRIVAL_TIMEOUT_S = 90.0

# Assembling one already-arriving stream is a local read off an open reader, so
# this is an outer bound on a stalled or half-open transfer, not a budget. Without
# it a reader that never yields and never closes keeps its assembly task, and the
# bytes it accumulated, alive for the rest of the session.
MATERIAL_ASSEMBLY_TIMEOUT_S = 30.0

# How long a start claim stays valid before it is treated as abandoned.
#
# The window between "the tool returned a supervisor" and "LiveKit activated it"
# is normally a single turn boundary. This is the outer bound on that window: a
# claim younger than this blocks a duplicate start, and one older than this is
# assumed to belong to a handoff that never happened, so the user can simply ask
# again instead of being locked out of Interview Mode for the rest of the call.
START_CLAIM_TTL_S = 30.0


class IntakeOutcome(StrEnum):
    """Why the intake task completed. Carried in the result, never on the task."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class InterviewIntakeResult:
    """The typed value ``InterviewIntakeTask`` completes with.

    Cancellation travels HERE, inside the awaited result, rather than as an
    attribute read off the task afterwards. A mutable side channel on the task is
    readable at moments the result is not yet meaningful, and it cannot carry the
    identity the supervisor needs to decide whether the result still applies.

    ``interview_id`` and ``ownership_epoch`` are stamped when the task is built,
    so a result completing after the session moved on is recognisably stale
    instead of being committed over whatever replaced it.
    """

    outcome: IntakeOutcome
    dossier: InterviewDossier
    interview_id: str
    ownership_epoch: int

    @property
    def cancelled(self) -> bool:
        return self.outcome is IntakeOutcome.CANCELLED
