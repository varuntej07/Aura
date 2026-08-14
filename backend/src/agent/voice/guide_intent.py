"""Fail-closed semantic admission decisions for desktop Guide Mode."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...lib.logger import logger
from ...services.model_provider import get_model_provider

GUIDE_INTENT_VERSION = "2026-08-13.1"
GUIDE_INTENT_DEADLINE_S = 2.0
GUIDE_INTENT_TTL_S = 45.0
GUIDE_START_MIN_SEMANTIC_CONFIDENCE = 0.82
GUIDE_START_MIN_STT_CONFIDENCE = 0.65


class GuideIntentRoute(StrEnum):
    START_GUIDE = "START_GUIDE"
    SCREEN_ANSWER_ONCE = "SCREEN_ANSWER_ONCE"
    CONTINUE_GUIDE = "CONTINUE_GUIDE"
    STOP_GUIDE = "STOP_GUIDE"
    CLARIFY = "CLARIFY"
    NORMAL = "NORMAL"


class GuideIntentReason(StrEnum):
    EXPLICIT_ONGOING_GUIDANCE = "explicit_ongoing_guidance"
    CONFIRMED_AFTER_GUIDE_OFFER = "confirmed_after_guide_offer"
    ONE_SHOT_SCREEN_QUESTION = "one_shot_screen_question"
    ACTIVE_GUIDE_CONTINUATION = "active_guide_continuation"
    EXPLICIT_STOP = "explicit_stop"
    AMBIGUOUS_GUIDANCE = "ambiguous_guidance"
    GARBLED_OR_INCOMPLETE = "garbled_or_incomplete"
    LOW_STT_CONFIDENCE = "low_stt_confidence"
    NORMAL_CONVERSATION = "normal_conversation"
    CLASSIFIER_TIMEOUT = "classifier_timeout"
    CLASSIFIER_FAILURE = "classifier_failure"
    INVALID_OUTPUT = "invalid_output"
    INVALID_EVIDENCE = "invalid_evidence"
    PREVIOUS_OFFER_NOT_CONFIRMED = "previous_offer_not_confirmed"
    GUIDE_ALREADY_ACTIVE = "guide_already_active"
    GUIDE_NOT_ACTIVE = "guide_not_active"


class GuideIntentModelOutput(BaseModel):
    """Provider response before it is bound to authoritative turn identity."""

    model_config = ConfigDict(extra="forbid")

    route: GuideIntentRoute
    task_summary: str = Field(max_length=240)
    evidence_quote: str = Field(max_length=500)
    semantic_confidence: float = Field(ge=0.0, le=1.0)
    depends_on_previous_assistant_offer: bool
    reason_code: GuideIntentReason


@dataclass(frozen=True, slots=True)
class GuideDecisionIdentity:
    message_id: str
    transcript_sha256: str
    turn_index: int
    guide_arm_epoch: int

    @classmethod
    def from_turn(
        cls,
        *,
        message_id: str,
        transcript: str,
        turn_index: int,
        guide_arm_epoch: int,
    ) -> GuideDecisionIdentity:
        return cls(
            message_id=message_id,
            transcript_sha256=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            turn_index=turn_index,
            guide_arm_epoch=guide_arm_epoch,
        )


@dataclass(frozen=True, slots=True)
class GuideIntentDecision:
    """One immutable proposal tied to one exact finalized desktop voice turn."""

    route: GuideIntentRoute
    task_summary: str
    evidence_quote: str
    semantic_confidence: float
    depends_on_previous_assistant_offer: bool
    previous_assistant_offer_confirmed: bool
    reason_code: GuideIntentReason
    identity: GuideDecisionIdentity
    decision_id: str
    issued_at_monotonic: float
    expires_at_monotonic: float
    valid: bool


@dataclass(frozen=True, slots=True)
class GuideAdmissionCheck:
    allowed: bool
    reason_code: str


_GUIDE_INTENT_SYSTEM = """You are a strict admission classifier for Aura desktop Guide Mode.
Return exactly one structured route. You propose a route; you never activate anything.

START_GUIDE only when the user clearly authorizes ongoing visual, step-by-step, or changing-screen assistance. Natural requests such as "walk me through this", "show me what to click", and "stay with me while I do this" are explicit authorization and do not need confirmation. An affirmative reply may be START_GUIDE only when previous_assistant_offered_guide is true and the immediately preceding assistant turn made that offer.

SCREEN_ANSWER_ONCE is a question or request about the currently visible screen that needs at most one fresh frame and does not ask for ongoing guidance. "Can you see this?", "what does this error say?", "is my API key visible?", "where is the Webhooks button?", and a bare "look at my screen" belong here or in CLARIFY, never START_GUIDE.

CONTINUE_GUIDE only when guide_active is true and the user continues the current visual task. STOP_GUIDE is an explicit request to stop, exit, cancel Guide, or stop watching. CLARIFY is for possibly procedural requests that are ambiguous, context-dependent without enough context, contradictory, garbled, incomplete, or meaningfully undermined by low STT confidence. NORMAL is social conversation, explanations without a request, ordinary questions, corrections, frustration, insults, and unrelated tasks.

Words such as screen, see, look, help, issue, error, here, this, or guide do not by themselves authorize Guide. Explaining an issue alone is NORMAL or SCREEN_ANSWER_ONCE. "Why did you turn Guide Mode on?", "you misunderstood me", and insults must never be START_GUIDE. A high-confidence nonsensical transcript is CLARIFY or NORMAL, not START_GUIDE.

evidence_quote must be copied verbatim as one exact substring of finalized_transcript. For START_GUIDE it must contain the explicit ongoing-guidance request or the affirmative reply. task_summary must be short and must not invent a task. semantic_confidence measures confidence in the route, not STT confidence."""

_CONTEXT_ONLY_AFFIRMATIVES = frozenset(
    {
        "yes",
        "yes please",
        "yeah",
        "yeah please",
        "yep",
        "yep please",
        "sure",
        "sure please",
        "okay",
        "ok",
        "please",
        "please do",
        "do it",
        "go ahead",
        "sounds good",
    }
)


def preceding_assistant_offered_guide(text: str) -> bool:
    """Recognize a narrow offer in Buddy-controlled output, never user intent."""
    normalized = " ".join(text.casefold().split())
    if not normalized or any(
        denial in normalized
        for denial in ("can't guide", "cannot guide", "won't guide", "not guide")
    ):
        return False
    guide_offer = any(
        phrase in normalized
        for phrase in (
            "guide you",
            "walk you through",
            "show you what to click",
            "stay with you while",
            "watch your screen",
            "keep watching",
        )
    ) or (
        "guide mode" in normalized
        and any(
            phrase in normalized
            for phrase in (
                "start guide mode",
                "turn on guide mode",
                "switch to guide mode",
                "activate guide mode",
                "use guide mode",
            )
        )
    )
    offer_language = any(
        phrase in normalized
        for phrase in (
            "would you like",
            "do you want me to",
            "want me to",
            "shall i",
            "i can",
            "let me",
        )
    )
    return guide_offer and offer_language


def context_only_affirmative(text: str) -> bool:
    normalized = "".join(
        character.casefold() if character.isalnum() or character.isspace() else " "
        for character in text
    )
    return " ".join(normalized.split()) in _CONTEXT_ONLY_AFFIRMATIVES


def guide_start_admission(
    decision: GuideIntentDecision | None,
    identity: GuideDecisionIdentity | None,
    *,
    now: float | None = None,
) -> GuideAdmissionCheck:
    current = guide_decision_currency(decision, identity, now=now)
    if not current.allowed:
        return current
    assert decision is not None
    if decision.route is not GuideIntentRoute.START_GUIDE:
        return GuideAdmissionCheck(False, f"guide_route_denied:{decision.route}")
    if not decision.evidence_quote:
        return GuideAdmissionCheck(False, "guide_evidence_missing")
    if (
        decision.depends_on_previous_assistant_offer
        and not decision.previous_assistant_offer_confirmed
    ):
        return GuideAdmissionCheck(False, "guide_previous_offer_unconfirmed")
    return GuideAdmissionCheck(True, "guide_start_admitted")


def guide_decision_currency(
    decision: GuideIntentDecision | None,
    identity: GuideDecisionIdentity | None,
    *,
    now: float | None = None,
) -> GuideAdmissionCheck:
    if decision is None:
        return GuideAdmissionCheck(False, "guide_decision_missing")
    if not decision.valid:
        return GuideAdmissionCheck(False, f"guide_decision_invalid:{decision.reason_code}")
    if identity is None or identity != decision.identity:
        return GuideAdmissionCheck(False, "guide_decision_identity_mismatch")
    if (time.monotonic() if now is None else now) > decision.expires_at_monotonic:
        return GuideAdmissionCheck(False, "guide_decision_expired")
    return GuideAdmissionCheck(True, "guide_decision_current")


class GuideIntentClassifier:
    def __init__(
        self,
        *,
        provider: Any | None = None,
        deadline_s: float = GUIDE_INTENT_DEADLINE_S,
        ttl_s: float = GUIDE_INTENT_TTL_S,
    ) -> None:
        self._provider = provider
        self._deadline_s = deadline_s
        self._ttl_s = ttl_s

    async def classify(
        self,
        *,
        transcript: str,
        identity: GuideDecisionIdentity,
        stt_confidence: float | None,
        guide_active: bool,
        previous_assistant_text: str,
        recent_dialogue: Sequence[dict[str, str]],
    ) -> GuideIntentDecision:
        previous_offer = preceding_assistant_offered_guide(previous_assistant_text)
        payload = {
            "finalized_transcript": transcript,
            "stt_confidence": stt_confidence,
            "guide_active": guide_active,
            "previous_assistant_offered_guide": previous_offer,
            "recent_dialogue": list(recent_dialogue)[-6:],
        }
        started = time.monotonic()
        try:
            provider = self._provider or get_model_provider()
            raw = await asyncio.wait_for(
                provider.balanced(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    system=_GUIDE_INTENT_SYSTEM,
                    response_model=GuideIntentModelOutput,
                    temperature=0.0,
                    max_output_tokens=220,
                ),
                timeout=self._deadline_s,
            )
            proposal = GuideIntentModelOutput.model_validate(raw)
        except TimeoutError:
            return self._failed_decision(
                identity, GuideIntentReason.CLASSIFIER_TIMEOUT, started
            )
        except asyncio.CancelledError:
            raise
        except ValidationError:
            return self._failed_decision(
                identity, GuideIntentReason.INVALID_OUTPUT, started
            )
        except Exception as exc:
            logger.warn(
                "GuideAdmission: classifier failed",
                {
                    "decision_identity": self._identity_log(identity),
                    "route": GuideIntentRoute.NORMAL,
                    "reason_code": GuideIntentReason.CLASSIFIER_FAILURE,
                    "error_type": type(exc).__name__,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
            return self._failed_decision(
                identity, GuideIntentReason.CLASSIFIER_FAILURE, started, log=False
            )

        route = proposal.route
        reason = proposal.reason_code
        valid = True
        evidence = proposal.evidence_quote
        depends_on_previous_offer = proposal.depends_on_previous_assistant_offer
        if evidence not in transcript or (route is GuideIntentRoute.START_GUIDE and not evidence):
            route = GuideIntentRoute.NORMAL
            reason = GuideIntentReason.INVALID_EVIDENCE
            valid = False
        elif route is GuideIntentRoute.START_GUIDE:
            depends_on_previous_offer = (
                depends_on_previous_offer or context_only_affirmative(transcript)
            )
            if guide_active:
                route = GuideIntentRoute.CONTINUE_GUIDE
                reason = GuideIntentReason.GUIDE_ALREADY_ACTIVE
            elif (
                stt_confidence is not None
                and stt_confidence < GUIDE_START_MIN_STT_CONFIDENCE
            ):
                route = GuideIntentRoute.CLARIFY
                reason = GuideIntentReason.LOW_STT_CONFIDENCE
            elif proposal.semantic_confidence < GUIDE_START_MIN_SEMANTIC_CONFIDENCE:
                route = GuideIntentRoute.CLARIFY
                reason = GuideIntentReason.AMBIGUOUS_GUIDANCE
            elif depends_on_previous_offer and not previous_offer:
                route = GuideIntentRoute.CLARIFY
                reason = GuideIntentReason.PREVIOUS_OFFER_NOT_CONFIRMED
            elif depends_on_previous_offer:
                reason = GuideIntentReason.CONFIRMED_AFTER_GUIDE_OFFER
            else:
                reason = GuideIntentReason.EXPLICIT_ONGOING_GUIDANCE
        elif route is GuideIntentRoute.CONTINUE_GUIDE and not guide_active:
            route = GuideIntentRoute.CLARIFY
            reason = GuideIntentReason.GUIDE_NOT_ACTIVE
        elif route is GuideIntentRoute.CONTINUE_GUIDE:
            reason = GuideIntentReason.ACTIVE_GUIDE_CONTINUATION
        elif route is GuideIntentRoute.SCREEN_ANSWER_ONCE:
            reason = GuideIntentReason.ONE_SHOT_SCREEN_QUESTION
        elif route is GuideIntentRoute.STOP_GUIDE:
            reason = GuideIntentReason.EXPLICIT_STOP
        elif route is GuideIntentRoute.CLARIFY and reason not in {
            GuideIntentReason.AMBIGUOUS_GUIDANCE,
            GuideIntentReason.GARBLED_OR_INCOMPLETE,
            GuideIntentReason.LOW_STT_CONFIDENCE,
        }:
            reason = GuideIntentReason.AMBIGUOUS_GUIDANCE
        elif route is GuideIntentRoute.NORMAL:
            reason = GuideIntentReason.NORMAL_CONVERSATION

        if route in {
            GuideIntentRoute.SCREEN_ANSWER_ONCE,
            GuideIntentRoute.CONTINUE_GUIDE,
            GuideIntentRoute.STOP_GUIDE,
            GuideIntentRoute.NORMAL,
        }:
            depends_on_previous_offer = False

        decision = self._decision(
            proposal=proposal,
            identity=identity,
            route=route,
            reason=reason,
            valid=valid,
            started=started,
            previous_offer=previous_offer,
            depends_on_previous_offer=depends_on_previous_offer,
        )
        self._log_decision(decision, started)
        return decision

    def _failed_decision(
        self,
        identity: GuideDecisionIdentity,
        reason: GuideIntentReason,
        started: float,
        *,
        log: bool = True,
    ) -> GuideIntentDecision:
        proposal = GuideIntentModelOutput(
            route=GuideIntentRoute.NORMAL,
            task_summary="",
            evidence_quote="",
            semantic_confidence=0.0,
            depends_on_previous_assistant_offer=False,
            reason_code=reason,
        )
        decision = self._decision(
            proposal=proposal,
            identity=identity,
            route=GuideIntentRoute.NORMAL,
            reason=reason,
            valid=False,
            started=started,
        )
        if log:
            self._log_decision(decision, started)
        return decision

    def _decision(
        self,
        *,
        proposal: GuideIntentModelOutput,
        identity: GuideDecisionIdentity,
        route: GuideIntentRoute,
        reason: GuideIntentReason,
        valid: bool,
        started: float,
        previous_offer: bool = False,
        depends_on_previous_offer: bool | None = None,
    ) -> GuideIntentDecision:
        issued = time.monotonic()
        identity_payload = (
            f"{GUIDE_INTENT_VERSION}|{identity.message_id}|"
            f"{identity.transcript_sha256}|{identity.turn_index}|"
            f"{identity.guide_arm_epoch}|{route}|{time.monotonic_ns()}"
        )
        decision_id = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()[:20]
        return GuideIntentDecision(
            route=route,
            task_summary=proposal.task_summary,
            evidence_quote=proposal.evidence_quote,
            semantic_confidence=proposal.semantic_confidence,
            depends_on_previous_assistant_offer=(
                proposal.depends_on_previous_assistant_offer
                if depends_on_previous_offer is None
                else depends_on_previous_offer
            ),
            previous_assistant_offer_confirmed=previous_offer,
            reason_code=reason,
            identity=identity,
            decision_id=decision_id,
            issued_at_monotonic=issued,
            expires_at_monotonic=issued + self._ttl_s,
            valid=valid,
        )

    def _log_decision(self, decision: GuideIntentDecision, started: float) -> None:
        logger.info(
            "GuideAdmission: turn classified",
            {
                "decision_id": decision.decision_id,
                "decision_identity": self._identity_log(decision.identity),
                "route": decision.route,
                "reason_code": decision.reason_code,
                "valid": decision.valid,
                "semantic_confidence": round(decision.semantic_confidence, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "deadline_ms": round(self._deadline_s * 1000),
            },
        )

    @staticmethod
    def _identity_log(identity: GuideDecisionIdentity) -> dict[str, object]:
        return {
            "message_id": identity.message_id,
            "transcript_sha256": identity.transcript_sha256,
            "turn_index": identity.turn_index,
            "guide_arm_epoch": identity.guide_arm_epoch,
        }
