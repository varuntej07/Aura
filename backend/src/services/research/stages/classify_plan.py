"""Decide what this run is asking and persist the plan it will execute.

One bounded model call, no tools. Everything expensive is downstream of this stage, and
that ordering is the product decision: scope is interpreted before a research credit is
debited and before a single Brave or Firecrawl call happens. Explicitly starting research
is authorization to proceed; any missing detail is recorded as an assumption instead of
parking the run after the user has walked away.

This stage never creates a search job. The transition from here to real work goes
through ``store.admit_run``, which debits the credit and creates the first search_wave
job in ONE transaction. Two routes to paid work, only one of which charges, is exactly
the shape that produces free runs.
"""

from __future__ import annotations

from typing import cast

from ....lib.logger import logger
from ...model_provider import get_model_provider
from .. import fields as F
from .. import policy as policy_mod
from ..llm_models import ClassificationResult, ClassifiedSubQuestion
from ..metering import (
    INPUT_TOKENS_PER_CALL,
    OUTPUT_TOKENS_PER_CALL,
    StageMeter,
    meter_models,
)
from ..models import EntityBindingStatus
from ..prompts import CLASSIFY_SYSTEM, classify_user_prompt
from ..sanitize import plain_text
from .base import StageContext, StageResult, StageResultKind

def _entity_key(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _sub_questions(
    raw: list[ClassifiedSubQuestion], objective: str, entities: list[str]
) -> list[dict[str, object]]:
    """Coerce the classifier's sub-questions into stable, id-bearing records.

    Ids are assigned HERE rather than trusted from the model, because they become the
    join key between claims, gaps and brief sections. A model that reuses or omits an id
    would silently merge two different questions' evidence.
    """
    entity_catalog: dict[str, list[str]] = {}
    for entity in entities:
        entity_catalog.setdefault(_entity_key(entity), []).append(entity)

    out: list[dict[str, object]] = []
    for index, item in enumerate(raw[:12]):
        text = plain_text(item.text.strip(), max_chars=500)
        if not text:
            continue
        requested = [
            plain_text(binding.entity, max_chars=120)
            for binding in item.entity_bindings
            if plain_text(binding.entity, max_chars=120)
        ]
        resolved: list[str] = []
        if not requested:
            binding_status = EntityBindingStatus.MISSING
        else:
            matches = [entity_catalog.get(_entity_key(binding), []) for binding in requested]
            if any(not match for match in matches):
                binding_status = EntityBindingStatus.MISMATCHED
            elif any(len(match) != 1 for match in matches):
                binding_status = EntityBindingStatus.AMBIGUOUS
            else:
                resolved = [match[0] for match in matches]
                binding_status = (
                    EntityBindingStatus.AMBIGUOUS
                    if len({_entity_key(entity) for entity in resolved}) != len(resolved)
                    else EntityBindingStatus.VALID
                )
        out.append({
            "sub_question_id": f"sq{index + 1}",
            "text": text,
            "must_answer": item.must_answer,
            "entity_bindings": [
                {"entity": entity} for entity in (resolved or requested)
            ],
            "entity_binding_status": binding_status.value,
        })
    if not out:
        # A classifier that decomposed nothing still leaves a runnable plan: the
        # objective itself becomes the single must-answer question.
        out.append({
            "sub_question_id": "sq1",
            "text": plain_text(objective, max_chars=500) or "Answer the request.",
            "must_answer": True,
            "entity_bindings": [],
            "entity_binding_status": EntityBindingStatus.MISSING.value,
        })
    return out


async def run(ctx: StageContext) -> StageResult:
    request_text = ctx.request_text.strip()
    # From the run document via StageContext, not from the job payload. The resume job
    # created by store.answer_clarification carries only resumed_from_question, so
    # reading the payload here meant a legacy resumed run never saw the answer it had
    # already collected. Keeping those answers in the prompt also preserves the correct
    # immutable plan version when an older parked run is resumed through the API.
    prior_answers = [
        str(answer.get("answer") if isinstance(answer, dict) else answer)
        for answer in ctx.clarification_answers
    ]
    round_index = len(prior_answers)

    provider = get_model_provider()
    meter = StageMeter()
    async with meter_models(
        meter,
        run_id=ctx.run_id,
        stage_kind=ctx.stage_kind,
        ctx=ctx,
        reserved_input_tokens_per_attempt=INPUT_TOKENS_PER_CALL,
        reserved_output_tokens_per_attempt=OUTPUT_TOKENS_PER_CALL,
    ):
        result = await provider.balanced(
            classify_user_prompt(request_text, prior_answers=prior_answers),
            system=CLASSIFY_SYSTEM,
            response_model=ClassificationResult,
            max_output_tokens=OUTPUT_TOKENS_PER_CALL,
            # No tools= argument. There is nothing for a request crafted to look like an
            # instruction to reach, the same guarantee the reading stage relies on.
        )
    if not isinstance(result, ClassificationResult):
        # A provider that returned prose instead of the schema is a retryable fault, not
        # a research outcome. Raising lets the engine's attempt cap and lease handle it.
        # The repo's usual pattern is a bare cast; the check is kept because a fallback
        # model dropping out of structured output is a real, observed failure mode.
        raise RuntimeError("classify_plan: provider did not return ClassificationResult")
    parsed = cast(ClassificationResult, result)

    # Guards 1 to 3 all live in policy.compose: an unknown id coerces to the conservative
    # generic row, low confidence ADDS that row rather than replacing the guess, and any
    # risk flag floors risk at medium and forces a disclaimer. A novel topic therefore
    # gets stricter handling than a recognised one, which is the opposite of the usual
    # fallback failure mode.
    effective_policy = policy_mod.compose(
        parsed.profile_ids,
        confidence=parsed.profile_confidence,
        risk_flags=list(parsed.risk_flags),
    )
    resolved_ids = policy_mod.resolve_profile_ids(
        parsed.profile_ids, confidence=parsed.profile_confidence
    )

    # Version is derived from the clarification round, not from a counter read. Round 0
    # is v1, and answering a question produces v2 alongside it rather than editing it.
    # Deterministic so a redelivered stage writes the same document id, where the
    # create-only write in the advance transaction refuses the duplicate outright.
    plan_version = round_index + 1
    entities = [plain_text(item, max_chars=120) for item in parsed.entities][:12]
    plan = {
        "plan_version": plan_version,
        "request_revision": round_index,
        "profile_ids": list(resolved_ids),
        "profile_confidence": parsed.profile_confidence,
        "objective": plain_text(parsed.objective, max_chars=1000),
        "entities": entities,
        "criteria": [plain_text(item, max_chars=200) for item in parsed.criteria][:10],
        "jurisdiction": plain_text(parsed.jurisdiction, max_chars=120),
        "language": parsed.language[:16] or "en",
        "time_anchor": plain_text(parsed.time_anchor, max_chars=64),
        "sub_questions": _sub_questions(parsed.sub_questions, parsed.objective, entities),
        "seed_queries": [plain_text(item, max_chars=200) for item in parsed.seed_queries][:12],
        "assumptions": [plain_text(item, max_chars=300) for item in parsed.assumptions][:6],
        "effective_policy": effective_policy.model_dump(mode="json"),
        "risk_flags": [plain_text(item, max_chars=64) for item in parsed.risk_flags][:6],
    }

    if parsed.needs_clarification and parsed.question:
        # A chat or voice turn must resolve essential ambiguity before it calls the tool.
        # A dashboard submission is already explicit authorization, so it proceeds on the
        # classifier's stated defaults and makes those assumptions visible in the result.
        defaults = [
            plain_text(item, max_chars=300)
            for item in parsed.question.default_assumptions
            if plain_text(item, max_chars=300)
        ]
        plan["assumptions"] = list(dict.fromkeys(plan["assumptions"] + defaults))[:6]
        logger.info(
            "research.classify_plan: proceeding on recorded assumptions",
            {"run_id": ctx.run_id, "default_count": len(defaults)},
        )

    actuals = meter.as_actuals()

    return StageResult(
        kind=StageResultKind.DONE,
        # queued, NOT searching. The durable marker lets the engine admit immediately
        # and lets the sweep recover a crash between this commit and that admission.
        next_state=F.STATE_QUEUED,
        documents={F.PLANS_SUBCOLLECTION: {str(plan_version): plan}},
        run_updates={
            F.CURRENT_PLAN_VERSION: plan_version,
            F.AUTO_ADMIT_REQUESTED: True,
        },
        stage_outputs={
            "profile_ids": list(resolved_ids),
            "profile_confidence": parsed.profile_confidence,
            "risk_flags": list(parsed.risk_flags),
            "sub_question_count": len(plan["sub_questions"]),
        },
        actuals=actuals,
        cost_microusd=meter.cost_microusd,
        cost_known=not meter.cost_incomplete,
    )
