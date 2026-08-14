"""Write the brief, then enforce its honesty in code.

The prompt asks for citations. The code REQUIRES them, and that difference is the whole
point of this stage. After parsing, and never in the prompt:

  * every cited claim_id must exist in the run's claims; unknown ids are stripped;
  * a section left with zero surviving citations is DELETED and becomes a gap;
  * a must-answer sub-question with no corroborated claim is force-added to gaps
    regardless of what the model wrote;
  * under hard recency, a dated, undated or stale claim cannot satisfy a must-answer.

A hallucinated gap-fill is therefore structurally discarded rather than argued with. The
model cannot get a fact into the brief without a persisted, excerpt-backed, URL-bearing
claim document behind it.

Exhaustion arrives here too. Every budget, wall-clock and cost-cap path routes through
this stage in ``partial`` mode instead of failing, because a sourced partial answer with
named gaps is the product and a bare failure is not.
"""

from __future__ import annotations

from typing import Any, cast

from ....lib.logger import logger
from ...model_provider import get_model_provider
from .. import fields as F
from ..eligibility import entity_binding_status
from ..llm_models import Brief
from ..metering import (
    OUTPUT_TOKENS_PER_CALL,
    SYNTHESIS_INPUT_TOKENS_RESERVE,
    StageMeter,
    meter_models,
)
from ..policy_table import DISCLAIMER_TEXT
from ..prompts import SYNTHESIZE_SYSTEM, synthesize_user_prompt
from .base import NextJob, StageContext, StageResult, StageResultKind

# Only these two can carry a must-answer. A disputed claim never satisfies one: presenting
# a contested value as the answer is exactly the failure the disagreement block exists to
# prevent.
_SATISFYING_CONFIDENCE = {"corroborated", "single_source"}


def _satisfies(
    claim: dict[str, Any], *, sub_question_id: str, hard_recency: bool, min_domains: int
) -> bool:
    if sub_question_id not in {
        str(item) for item in (claim.get("eligible_sub_question_ids") or ())
    }:
        return False
    if claim.get("confidence") not in _SATISFYING_CONFIDENCE:
        return False
    if claim.get("policy_shortfalls"):
        # The policy named a KIND of source this claim does not have: a regulator, a
        # standards body, the primary record, an independent one. verify records the
        # shortfall and downgrades the claim to single_source, and single_source is in
        # _SATISFYING_CONFIDENCE, so without this line the requirement was computed,
        # written to the claim, and then ignored at the only point that consumes it.
        # A claim like this can still be REPORTED with its sources; it cannot be the
        # answer to a question the policy said needs better evidence.
        return False
    if int(claim.get("support_domains") or 0) < min_domains:
        return False
    if hard_recency and claim.get("freshness") != "current":
        # "The lift was step-free in 2019" is not evidence about today. Under a hard rule
        # a non-current claim is reportable with an as-of qualifier but cannot answer.
        return False
    return True


# Trim order when a claim set does not fit the granted prompt budget: the weakest evidence
# goes first, so what survives is what the brief could most defensibly have said anyway.
_CONFIDENCE_ORDER = {
    "corroborated": 0,
    "single_source": 1,
    "disputed": 2,
    "unverified": 3,
}

# Characters per token, for sizing only. Deliberately a rough constant rather than a
# tokenizer: a real count needs one tokenizer per provider plus a re-count on every
# fallback hop, and being wrong there fails in the dangerous direction (refusing a call
# that would have fit). Four is the conventional English estimate and it over-counts
# structured text, which errs toward a smaller prompt.
_CHARS_PER_TOKEN = 4


def _fit_claims(
    claims: list[dict[str, Any]], token_budget: int
) -> tuple[list[dict[str, Any]], int]:
    """Take as many claims as the token budget allows. Returns (kept, trimmed_count)."""
    if token_budget <= 0:
        return list(claims), 0
    char_budget = token_budget * _CHARS_PER_TOKEN
    kept: list[dict[str, Any]] = []
    used = 0
    for claim in claims:
        # The rendered size of one claim block in synthesize_user_prompt, plus slack for
        # the field labels around it.
        size = len(str(claim.get("text") or "")) + len(str(claim.get("claim_id") or "")) + 160
        if used + size > char_budget and kept:
            break
        kept.append(claim)
        used += size
    return kept, len(claims) - len(kept)


def _render_statements(
    statements: Any, known: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int]:
    """Render selected claims into atomic statements. Returns (kept, dropped, stripped).

    The factual text comes from the claim. No model-authored connective survives.

    A statement whose ids all fail to resolve is dropped rather than kept uncited, which
    is the same rule as before with the loophole closed: previously the prose survived the
    id check because the prose was the model's own and only the ids were verified.
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    stripped = 0
    for statement in statements or ():
        ids = list(getattr(statement, "claim_ids", ()) or ())
        surviving = [cid for cid in ids if cid in known]
        stripped += len(ids) - len(surviving)
        if not surviving:
            dropped += 1
            continue
        rendered = " ".join(
            str(known[cid].get("text") or "").strip() for cid in surviving
        ).strip()
        if not rendered:
            # A resolved id whose claim carries no text is not a statement. This is the
            # structural floor: no claim text, no sentence, whatever the citation says.
            dropped += 1
            continue
        kept.append({
            "text": _qualified_claim_text(rendered, [known[cid] for cid in surviving]),
            "claim_ids": surviving[:3],
        })
    return kept, dropped, stripped


def _qualified_claim_text(rendered: str, claims: list[dict[str, Any]]) -> str:
    """Append code-owned evidence status from persisted metadata."""
    qualifiers: list[str] = []
    if any(claim.get("confidence") == "disputed" for claim in claims):
        qualifiers.append("Status: disputed")
    scopes = {
        (str(scope.get("dimension") or ""), str(scope.get("value") or ""))
        for claim in claims
        if isinstance((scope := claim.get("scope")), dict)
        and scope.get("dimension") and scope.get("value")
    }
    qualifiers.extend(
        f"Scope: {dimension.replace('_', ' ')}={value}"
        for dimension, value in sorted(scopes)
    )
    freshness = {str(claim.get("freshness") or "") for claim in claims}
    if "stale" in freshness:
        qualifiers.append("Status: stale")
    elif "dated" in freshness:
        qualifiers.append("Status: dated")
    elif "undated" in freshness:
        qualifiers.append("Source date: undated")
    if any(claim.get("superseded_by") for claim in claims):
        qualifiers.append("Relationship: superseded by newer evidence")
    as_of = sorted({str(claim.get("as_of") or "")[:10] for claim in claims if claim.get("as_of")})
    qualifiers.extend(f"As of {value}" for value in as_of)
    return f"{rendered} ({'; '.join(qualifiers)})." if qualifiers else rendered


def _status_section(claims: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Render every disputed, scoped, stale, or superseded relationship without a model."""
    status_ids = {
        claim_id for claim_id, claim in claims.items()
        if claim.get("confidence") == "disputed"
        or claim.get("scope")
        or claim.get("freshness") in ("dated", "undated", "stale")
        or claim.get("superseded_by")
    }
    statements: list[dict[str, Any]] = []
    disagreements: list[str] = []
    rendered_ids: set[str] = set()
    for claim_id in sorted(status_ids):
        if claim_id in rendered_ids:
            continue
        claim = claims[claim_id]
        related = [claim_id]
        related.extend(
            other for other in (claim.get("contradicts") or ()) if other in claims
        )
        winner = str(claim.get("superseded_by") or "")
        if winner in claims:
            related.append(winner)
        related = list(dict.fromkeys(related))
        rendered_ids.update(related)
        rendered = " ".join(str(claims[item].get("text") or "").strip() for item in related)
        statements.append({
            "text": _qualified_claim_text(rendered, [claims[item] for item in related]),
            "claim_ids": related,
        })
        if claim.get("confidence") == "disputed":
            disagreements.append(
                "Disputed evidence remains between "
                + " and ".join(f"claim {item}" for item in sorted(related))
                + "."
            )
    if not statements:
        return None, []
    return {
        "heading": "Evidence status",
        "statements": statements,
        "claim_ids": sorted({item for statement in statements for item in statement["claim_ids"]}),
    }, list(dict.fromkeys(disagreements))[:10]


def _supplemental_section(
    claims: dict[str, dict[str, Any]], excluded_ids: set[str]
) -> dict[str, Any] | None:
    statements = []
    for claim_id, claim in sorted(claims.items()):
        if claim_id in excluded_ids or claim.get("eligible_sub_question_ids"):
            continue
        statements.append({
            "text": "Supplemental evidence only: "
            + _qualified_claim_text(str(claim.get("text") or ""), [claim]),
            "claim_ids": [claim_id],
        })
    if not statements:
        return None
    return {
        "heading": "Supplemental evidence",
        "statements": statements,
        "claim_ids": [item["claim_ids"][0] for item in statements],
    }


async def run(ctx: StageContext) -> StageResult:
    # Imported inside the function, not at module scope: store imports stages.base,
    # so a module-level import here would close a cycle (store -> stages -> registry
    # -> this module -> store) and leave store half-initialised.
    from .. import store

    plan = ctx.plan or {}
    policy = dict(plan.get("effective_policy") or {})
    recency = dict(policy.get("recency") or {})
    hard_recency = bool(recency.get("hard"))
    min_domains = int(policy.get("min_corroboration", 1) or 1)
    sections_wanted = tuple(policy.get("output_sections") or ("Summary",))

    mode = str(ctx.payload.get("mode") or "full")
    stored_claims = await store.list_documents(ctx.uid, ctx.run_id, F.CLAIMS_SUBCOLLECTION)
    known: dict[str, dict[str, Any]] = {
        str(claim.get("claim_id") or claim.get("doc_id")): claim for claim in stored_claims
    }

    sub_questions = list(plan.get("sub_questions") or [])
    must_answer = [item for item in sub_questions if item.get("must_answer")]

    # Computed BEFORE the model runs, from stored claims only. This is the ground truth
    # the model's output is checked against, not a second opinion derived from it.
    unanswered: list[dict[str, str]] = []
    binding_failures: set[str] = set()
    for item in sub_questions:
        sub_id = str(item.get("sub_question_id") or "")
        status = entity_binding_status(sub_question_id=sub_id, plan=plan)
        if status.value == "valid":
            continue
        binding_failures.add(sub_id)
        unanswered.append({
            "sub_question_id": sub_id,
            "text": f"Entity binding is {status.value}; evidence remains supplemental.",
            "reason": F.FAIL_ENTITY_BINDING,
        })
    for item in must_answer:
        sub_id = str(item.get("sub_question_id") or "")
        if sub_id in binding_failures:
            continue
        supporting = [
            claim
            for claim in known.values()
            if _satisfies(
                claim,
                sub_question_id=sub_id,
                hard_recency=hard_recency,
                min_domains=min_domains,
            )
        ]
        if supporting:
            continue
        disputed = any(
            sub_id in {str(value) for value in (claim.get("eligible_sub_question_ids") or ())}
            and claim.get("confidence") == "disputed"
            for claim in known.values()
        )
        stale = any(
            sub_id in {str(value) for value in (claim.get("eligible_sub_question_ids") or ())}
            and claim.get("freshness") in ("dated", "undated", "stale")
            for claim in known.values()
        )
        # Which source requirements the claims for this sub-question actually failed.
        # Named rather than folded into "no source found", because "we found nothing"
        # and "we found blogs where the policy demands a regulator" are different
        # answers and only the second tells the user what would fix it.
        shortfalls = sorted({
            str(item)
            for claim in known.values()
            if sub_id in {str(value) for value in (claim.get("eligible_sub_question_ids") or ())}
            for item in (claim.get("policy_shortfalls") or ())
        })
        if disputed:
            reason = F.FAIL_CONTRADICTORY
        elif stale and hard_recency:
            reason = F.FAIL_NO_CURRENT_SOURCE
        elif shortfalls:
            reason = F.FAIL_SOURCE_POLICY_UNMET
        else:
            reason = F.FAIL_NO_SOURCE_FOUND
        unanswered.append({
            "sub_question_id": sub_id,
            "text": "Required evidence was not positively admitted for this question.",
            "reason": reason,
        })

    # Consume the token grant this stage was actually given, rather than reserving against
    # it and then building whatever prompt the claim set happens to produce.
    #
    # Synthesis receives every claim in the run, so it is the stage whose prompt size is
    # least predictable and the one most likely to exceed a degraded grant. It read
    # ctx.grant nowhere, so a short grant was reserved, ignored, and then recorded as an
    # overrun after the tokens had been spent. The claim list is trimmed to fit instead,
    # lowest-confidence first, and what was dropped is reported rather than hidden.
    from ..metering import RESEARCH_ATTEMPT_BUDGET

    granted_input = int(ctx.grant.get(F.UNIT_MODEL_INPUT_TOKENS) or 0)
    prompt_token_budget = granted_input // max(1, RESEARCH_ATTEMPT_BUDGET)
    status_section, disagreements = _status_section(known)
    status_ids = set(status_section["claim_ids"]) if status_section else set()
    ordinary_claims = {
        claim_id: claim for claim_id, claim in known.items()
        if claim.get("eligible_sub_question_ids") and claim_id not in status_ids
    }
    ordered_claims = sorted(
        ordinary_claims.values(),
        key=lambda claim: _CONFIDENCE_ORDER.get(str(claim.get("confidence") or ""), 9),
    )
    prompt_claims, claims_trimmed = _fit_claims(ordered_claims, prompt_token_budget)
    if claims_trimmed:
        logger.info(
            "research.synthesize: claim set trimmed to fit the granted token budget",
            {"run_id": ctx.run_id, "claims": len(known), "kept": len(prompt_claims),
             "trimmed": claims_trimmed, "granted_input_tokens": granted_input},
        )

    provider = get_model_provider()
    meter = StageMeter()

    async with meter_models(
        meter,
        run_id=ctx.run_id,
        stage_kind=ctx.stage_kind,
        ctx=ctx,
        reserved_input_tokens_per_attempt=SYNTHESIS_INPUT_TOKENS_RESERVE,
        reserved_output_tokens_per_attempt=OUTPUT_TOKENS_PER_CALL,
    ):
        result = await provider.expert(
            synthesize_user_prompt(
                objective=str(plan.get("objective", "")),
                mode=mode,
                sections=sections_wanted,
                claims=prompt_claims,
                unanswered=unanswered,
            ),
            system=SYNTHESIZE_SYSTEM,
            response_model=Brief,
            max_output_tokens=OUTPUT_TOKENS_PER_CALL,
        )
    if not isinstance(result, Brief):
        raise RuntimeError("synthesize: provider did not return Brief")
    brief = cast(Brief, result)

    # --- post-parse enforcement -----------------------------------------------------
    #
    # The factual content is RENDERED FROM STORED CLAIMS, not copied from the model. The
    # model chose which claims to state and in what order; every word of fact below comes
    # from a claim document whose text was itself derived from an excerpt verified
    # verbatim against the fetched page. A claim id that does not resolve therefore does
    # not merely lose its citation, it removes the sentence, because there is no sentence
    # without it.
    sections: list[dict[str, Any]] = []
    dropped_sections = 0
    dropped_statements = 0
    stripped_ids = 0
    for section in brief.sections:
        kept, dropped, stripped = _render_statements(section.statements, ordinary_claims)
        dropped_statements += dropped
        stripped_ids += stripped
        if not kept:
            # A section with nothing left is not trimmed, it is deleted. Leaving it would
            # put an uncited assertion in a brief whose whole promise is the opposite.
            dropped_sections += 1
            continue
        section_index = int(section.section_index)
        if section_index >= len(sections_wanted):
            dropped_sections += 1
            continue
        sections.append({
            "heading": str(sections_wanted[section_index]),
            "statements": kept,
            # Flattened union, kept for readers that just want "what backs this section".
            "claim_ids": sorted({cid for item in kept for cid in item["claim_ids"]}),
        })

    # The summary is held to exactly the rule the body is held to, and rendered the same
    # way. A summary that resolves to no statement is replaced with a notice rather than
    # published, because the summary is the part most likely to be quoted alone.
    summary_statements, summary_dropped, summary_stripped = _render_statements(
        brief.executive_summary, ordinary_claims
    )
    dropped_statements += summary_dropped
    stripped_ids += summary_stripped
    summary_ids = sorted({cid for item in summary_statements for cid in item["claim_ids"]})
    summary_uncited = bool(known) and not summary_statements
    executive_summary = " ".join(item["text"] for item in summary_statements).strip()
    if summary_uncited or not executive_summary:
        executive_summary = (
            "A summary was withheld: the model did not tie it to any verified claim. "
            "The cited sections below carry what the run could actually establish."
        )
        summary_uncited = bool(known)

    gaps: list[dict[str, str]] = []
    seen_gaps: set[tuple[str, str]] = set()

    # partial_over_guess is True on every policy row, so this is unconditional: an
    # unsupported must-answer becomes a gap whatever the model claimed about it.
    for item in unanswered:
        key = (item["sub_question_id"], item["reason"])
        if key in seen_gaps:
            continue
        seen_gaps.add(key)
        gaps.append({
            "sub_question_id": item["sub_question_id"],
            "reason": item["reason"],
            "detail": item["text"],
        })
    if dropped_sections or dropped_statements:
        removed = []
        if dropped_sections:
            removed.append(f"{dropped_sections} section(s)")
        if dropped_statements:
            removed.append(f"{dropped_statements} statement(s)")
        gaps.append({
            "sub_question_id": "",
            "reason": F.FAIL_NO_SOURCE_FOUND,
            "detail": f"{' and '.join(removed)} removed for having no surviving citation",
        })

    supplemental_section = _supplemental_section(known, status_ids)
    if status_section:
        sections.append(status_section)
    if supplemental_section:
        sections.append(supplemental_section)

    # Composed by UNION across every policy row that applied, which is why disclaimer_keys
    # is plural: a patient-treatment question carries both the clinical and the scientific
    # disclaimer. Rendered here, in code, rather than asked for in the prompt, for the
    # same reason citations are enforced here: a legally material line must not depend on
    # the model remembering to write it.
    disclaimers = [
        DISCLAIMER_TEXT[key]
        for key in (policy.get("disclaimer_keys") or ())
        if key in DISCLAIMER_TEXT
    ]
    if policy.get("requires_disclaimer") and not disclaimers:
        # The policy demanded one and named none we recognise. Fall back rather than
        # ship a high-risk answer bare.
        disclaimers = [DISCLAIMER_TEXT["general_information"]]

    document = {
        "executive_summary": executive_summary,
        "executive_summary_claim_ids": summary_ids,
        "sections": sections,
        "gaps": gaps,
        "disagreements": disagreements,
        "disclaimers": disclaimers,
        "assumptions": [],
        "mode": mode,
        "claim_count": len(known),
        "cited_claim_ids": sorted(
            {cid for section in sections for cid in section["claim_ids"]} | set(summary_ids)
        ),
    }

    # At least one surviving evidence-backed statement is REQUIRED before ready.
    #
    # This is the floor the previous rule was missing. `complete` asked whether anything
    # had been DROPPED, so a run that found nothing at all and wrote no sections dropped
    # nothing, had no must-answer left unanswered when the plan named none, and sailed
    # through to READY carrying zero evidence. A brief whose entire promise is "every
    # claim is backed by a URL and an excerpt" must never reach its success state having
    # backed nothing.
    evidence_statements = sum(len(section["statements"]) for section in sections)
    evidence_statements += len(summary_statements)
    answer_statements = sum(
        len(section["statements"])
        for section in sections
        if section["heading"] not in ("Evidence status", "Supplemental evidence")
    ) + len(summary_statements)
    if evidence_statements <= 0:
        # Deterministic, machine-assigned, and added regardless of what the model wrote.
        key = ("", F.FAIL_NO_SOURCE_FOUND)
        if key not in seen_gaps:
            seen_gaps.add(key)
            gaps.append({
                "sub_question_id": "",
                "reason": F.FAIL_NO_SOURCE_FOUND,
                "detail": "No source-backed statement survived verification.",
            })
        document["gaps"] = gaps

    # ready ONLY when every must-answer is covered, nothing was dropped, and the brief
    # actually says something a source supports. Anything else is partial, and partial is
    # a first-class, useful outcome rather than a soft failure.
    complete = (
        answer_statements > 0
        and not unanswered
        and not dropped_sections
        and not dropped_statements
        and not summary_uncited
        and mode == "full"
    )
    terminal_state = F.STATE_READY if complete else F.STATE_PARTIAL

    return StageResult(
        kind=StageResultKind.DONE,
        # finalize owns the terminal commit and the notification job, so that the result
        # and its delivery intent land in ONE transaction.
        next_state=F.STATE_SYNTHESIZING,
        next_jobs=(
            NextJob(
                stage_kind=F.STAGE_FINALIZE,
                wave=ctx.wave,
                payload={"terminal_state": terminal_state},
            ),
        ),
        run_updates={F.BRIEF: document, F.GAPS: gaps},
        stage_outputs={
            "mode": mode,
            "sections": len(sections),
            "sections_dropped": dropped_sections,
            "statements_dropped": dropped_statements,
            "evidence_statements": evidence_statements,
            "summary_uncited": summary_uncited,
            "disclaimers": len(disclaimers),
            "claim_ids_stripped": stripped_ids,
            "gaps": len(gaps),
            "terminal_state": terminal_state,
            "cost_incomplete": meter.cost_incomplete,
        },
        actuals=meter.as_actuals(),
        cost_microusd=meter.cost_microusd,
        cost_known=not meter.cost_incomplete,
    )
