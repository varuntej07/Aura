"""Merge candidate claims into consolidated claims, and adjudicate disagreements.

The SINGLE owner of the ``claims`` subcollection. Children write candidates onto their
own source documents and never touch a claim, so this stage is the only writer and the
merge is deterministic: no contention, no lost evidence-array update, no race.

Corroboration is counted in DISTINCT PUBLISHERS (eTLD+1), not in sources and not in
hostnames. Three pages on one site corroborate nothing, three SUBDOMAINS of one site
corroborate nothing either, and an aggregator or a quarantined page counts for nothing
at all.

Counting is only half of it. The composed policy also says what KIND of source is
required: groups that must each be represented, a primary record, an independent source,
and the waiver classes that stand alone. A claim that clears the count but fails any of
those is single_source, not corroborated, and it records exactly which requirement it
failed so the brief can name the gap.

Contradictions are never silently resolved. Two sources giving different values for the
same (subject, attribute) go to one bounded adjudication call that may only classify the
RELATIONSHIP; it cannot write a new value. An unexplained difference stays disputed, both
values stay visible with their dates, and a disputed value can never satisfy a
must-answer sub-question.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from ....lib.logger import logger
from ...model_provider import get_model_provider
from .. import fields as F
from ..domain_class import independence_key
from ..eligibility import validate_scope_qualifier
from ..llm_models import Adjudication, AdjudicationVerdict
from ..metering import (
    RESEARCH_ATTEMPT_BUDGET,
    VERIFY_INPUT_TOKENS_PER_CALL,
    VERIFY_OUTPUT_TOKENS_PER_CALL,
    StageMeter,
    meter_models,
)
from ..models import ScopeDimension
from ..policy_table import (
    NON_CORROBORATING_CLASSES,
    NON_INDEPENDENT_CLASSES,
    PRIMARY_SOURCE_CLASSES,
    SourceClass,
)
from ..prompts import ADJUDICATE_SYSTEM, adjudicate_user_prompt
from .base import NextJob, StageContext, StageResult, StageResultKind

# Ceilings on the fan-out of adjudication calls. A pathological run with forty disputed
# attributes must not spend forty model calls discovering that they disagree.
ADJUDICATION_MAX = 6
CLAIMS_MAX = 120

# Attributes whose value is EXPECTED to change over time, so "the newer source wins" is a
# meaningful statement about them. Everything else is a contradiction when two sources
# disagree, however confidently the adjudicator labelled it superseded.
#
# Matched on substrings of the model-normalized attribute name (which reads like a field
# name by construction: monthly_price_usd_starter, headcount, version). This is a
# code-owned list of MEASURE SHAPES, not of topics: it says which kinds of quantity have a
# time dimension, and contains no subject, domain or industry.
_TIME_VARYING_ATTRIBUTE_HINTS = (
    "price", "cost", "fee", "rate", "salary", "wage", "revenue", "arr", "mrr",
    "valuation", "funding", "headcount", "employees", "users", "customers", "count",
    "share", "ranking", "rank", "version", "release", "status", "availability",
    "deadline", "effective", "expiry", "expires", "as_of", "current", "latest",
    "population", "score", "capacity", "limit", "quota", "balance", "total",
)

# Normalizations applied before two values are called equivalent. Deliberately small and
# deterministic: an "agreement" verdict MERGES two sources into one claim, so the test for
# it has to be something code can re-derive, not something a model asserted.
_VALUE_STRIP = " \t\n\r$£€,%"
_UNIT_ALIASES = {
    "usd": "$", "dollars": "$", "dollar": "$", "eur": "€", "gbp": "£",
    "percent": "%", "pct": "%",
    "per month": "/mo", "monthly": "/mo", "a month": "/mo", "/month": "/mo",
    "per year": "/yr", "yearly": "/yr", "annually": "/yr", "/year": "/yr",
    "thousand": "e3", "k": "e3", "million": "e6", "m": "e6", "billion": "e9",
}


def _canonical_value(raw: str) -> str:
    """A deterministic normal form for comparing two claimed values.

    Only whitespace, case, currency symbols, thousands separators and a fixed alias table
    are collapsed. Nothing here interprets meaning, because the moment this starts making
    judgements it stops being a check on the model and becomes a second, unreviewed model.
    """
    text = " ".join((raw or "").split()).casefold()
    for alias, canon in _UNIT_ALIASES.items():
        text = text.replace(alias, canon)
    return text.strip(_VALUE_STRIP).replace(" ", "")


def _values_equivalent(values: list[str]) -> bool:
    """True when every competing value reduces to the same canonical form."""
    canon = {_canonical_value(value) for value in values}
    canon.discard("")
    return len(canon) <= 1


def _grounded_scope_bindings(
    parsed: Adjudication, candidates: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], str]:
    """Resolve only prevalidated qualifier ids, then revalidate their source evidence."""
    expected_ids = [f"candidate_{index}" for index in range(1, len(candidates) + 1)]
    references = list(parsed.scope_references)
    by_id = {reference.candidate_id: reference for reference in references}
    if len(references) != len(candidates) or set(by_id) != set(expected_ids):
        return [], "scope_candidates_incomplete"
    grounded: list[dict[str, str]] = []
    for candidate_id, candidate in zip(expected_ids, candidates, strict=True):
        reference = by_id[candidate_id]
        qualifiers = list(candidate.get("scope_qualifiers") or ())
        qualifier_by_id = {
            str(item.get("qualifier_id") or ""): item
            for item in qualifiers
            if isinstance(item, dict) and str(item.get("qualifier_id") or "")
        }
        if len(qualifier_by_id) != len(qualifiers):
            return [], "scope_qualifier_ids_invalid"
        qualifier = qualifier_by_id.get(reference.qualifier_id)
        if qualifier is None:
            return [], "scope_qualifier_not_prevalidated"
        try:
            dimension = ScopeDimension(str(qualifier.get("dimension") or ""))
        except ValueError:
            return [], "scope_dimension_invalid"
        validated = validate_scope_qualifier(
            dimension=dimension,
            value=str(qualifier.get("value") or ""),
            evidence_excerpt=str(qualifier.get("evidence_excerpt") or ""),
            authoritative_excerpt=str(candidate.get("excerpt") or ""),
        )
        if validated is None:
            return [], "scope_qualifier_not_grounded"
        grounded.append(validated)
    dimensions = {item["dimension"] for item in grounded}
    if len(dimensions) != 1:
        return [], "scope_dimensions_incompatible"
    values = {_canonical_value(item["value"]) for item in grounded}
    if "" in values or len(values) != len(grounded):
        return [], "scope_values_not_distinct"
    return grounded, ""


def _attribute_varies_over_time(attribute: str) -> bool:
    lowered = (attribute or "").casefold()
    return any(hint in lowered for hint in _TIME_VARYING_ATTRIBUTE_HINTS)


def _claim_id(subject: str, attribute: str, value: str) -> str:
    key = f"{subject.casefold()}|{attribute.casefold()}|{value.casefold()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _parse_dt(raw: str) -> datetime | None:
    """Parse a page-declared date, tolerantly. A bad date is no date, never today."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%Y/%m/%d", "%Y"):
            try:
                parsed = datetime.strptime(text[:32], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _freshness(published: datetime | None, max_age_days: int, now: datetime) -> str:
    """Per CLAIM, not per run. Absence is reported as undated, never guessed as current."""
    if published is None:
        return "undated"
    if max_age_days <= 0:
        return "current"
    return "current" if published >= now - timedelta(days=max_age_days) else "dated"


def _classes_from(raw: Any) -> set[SourceClass]:
    """Coerce a policy's class list to the enum, dropping anything unrecognised."""
    out: set[SourceClass] = set()
    for value in raw or ():
        try:
            out.add(SourceClass(str(value)))
        except ValueError:
            continue
    return out


def _policy_shortfalls(
    members: list[dict[str, Any]], policy: dict[str, Any]
) -> list[str]:
    """Which of the policy's SOURCE requirements this claim's evidence fails.

    Everything here was declared in ``policy_table`` and read by nothing. Corroboration
    counted domains and recency, so a row demanding "2, at least one REGULATOR_GOV", or
    a primary record, or an independent source, was satisfied by any two blogs.

    Only corroborating members are considered: an aggregator or a quarantined page cannot
    satisfy a requirement it is not allowed to count towards in the first place.
    """
    classes = {member["source_class"] for member in members}
    shortfalls: list[str] = []

    # A conjunction of disjunctions: EVERY group needs one member. Composition
    # concatenates groups precisely so that composing two policies is STRICTER than
    # either, which a flattened union would silently invert.
    for group in policy.get("required_source_class_groups") or ():
        wanted = _classes_from(group)
        if wanted and not (classes & wanted):
            shortfalls.append(
                "missing_" + "_or_".join(sorted(item.value for item in wanted))
            )

    if policy.get("primary_source_required") and not (
        classes & set(PRIMARY_SOURCE_CLASSES)
    ):
        shortfalls.append("missing_primary_source")

    if policy.get("independence_required") and not (
        classes - set(NON_INDEPENDENT_CLASSES)
    ):
        shortfalls.append("missing_independent_source")

    return shortfalls


def _enforce_invariants(
    parsed: Adjudication, candidates: list[dict[str, Any]]
) -> tuple[str, str, list[dict[str, str]], str]:
    """Check the adjudicator's verdict against evidence code can re-derive itself.

    Returns ``(verdict, winning_value, scopes, violation)``. A non-empty ``violation``
    means the claimed verdict did not survive and the result is a contradiction.

    The model's enum is a claim ABOUT the values, not a fact about them, and three of the
    four verdicts change what the user is shown. Trusting the enum alone meant a model
    could silently merge two different numbers by saying "agreement", or mark a source
    stale by saying "superseded" with no date anywhere in the evidence. Contradiction is
    the safe fallback because it is the one verdict that resolves nothing.
    """
    verdict = str(parsed.verdict)
    winning = parsed.winning_value.strip()
    values = [str(item.get("value_normalized") or "") for item in candidates]
    attribute = str(candidates[0].get("attribute") or "") if candidates else ""

    if verdict == AdjudicationVerdict.AGREEMENT:
        # A merge, so the values must actually reduce to the same thing. "$20/mo" and
        # "twenty dollars a month" do; "$20/mo" and "$25/mo" do not, and calling them
        # agreement would publish one price and delete the other without telling anyone.
        if not _values_equivalent(values):
            return AdjudicationVerdict.CONTRADICTION, "", [], "values_not_equivalent"
        return verdict, "", [], ""

    if verdict == AdjudicationVerdict.DIFFERENT_SCOPE:
        scopes, violation = _grounded_scope_bindings(parsed, candidates)
        if violation:
            return AdjudicationVerdict.CONTRADICTION, "", [], violation
        return verdict, "", scopes, ""

    if verdict == AdjudicationVerdict.SUPERSEDED_BY_RECENCY:
        # The adjudicator may only classify a RELATIONSHIP between values that already
        # carry evidence; it cannot write a new one. A winning_value matching none of them
        # is exactly that forbidden move, and acting on it would mint a superseded_by
        # pointing at a claim id no claim will ever have.
        canon_by_value = {_canonical_value(value): value for value in values}
        if _canonical_value(winning) not in canon_by_value:
            return AdjudicationVerdict.CONTRADICTION, "", [], "winner_not_among_values"
        # "Newer" is only meaningful if both sides are dated. An undated page is not old,
        # it is undated, and treating it as superseded marks a possibly-current fact stale.
        dated = [
            (item, _parse_dt(str(item.get("published_at") or "")))
            for item in candidates
        ]
        if any(when is None for _item, when in dated):
            return AdjudicationVerdict.CONTRADICTION, "", [], "undated_candidate"
        winner_dates = [
            when
            for item, when in dated
            if _canonical_value(str(item.get("value_normalized") or ""))
            == _canonical_value(winning)
            and when is not None
        ]
        loser_dates = [
            when
            for item, when in dated
            if _canonical_value(str(item.get("value_normalized") or ""))
            != _canonical_value(winning)
            and when is not None
        ]
        if not winner_dates or not loser_dates:
            return AdjudicationVerdict.CONTRADICTION, "", [], "no_comparable_dates"
        if not max(winner_dates) > max(loser_dates):
            # STRICTLY newer. Equal dates are two sources disagreeing on the same day,
            # which is the textbook contradiction.
            return AdjudicationVerdict.CONTRADICTION, "", [], "winner_not_strictly_newer"
        if not _attribute_varies_over_time(attribute):
            # A founding date or a chemical constant does not get superseded; two sources
            # disagreeing about one are simply disagreeing.
            return AdjudicationVerdict.CONTRADICTION, "", [], "attribute_is_not_temporal"
        return verdict, winning, [], ""

    return AdjudicationVerdict.CONTRADICTION, "", [], ""


async def run(ctx: StageContext) -> StageResult:
    # Imported inside the function, not at module scope: store imports stages.base,
    # so a module-level import here would close a cycle (store -> stages -> registry
    # -> this module -> store) and leave store half-initialised.
    from .. import store

    plan = ctx.plan or {}
    policy = dict(plan.get("effective_policy") or {})
    recency = dict(policy.get("recency") or {})
    max_age_days = int(recency.get("max_age_days", 0) or 0)
    min_corroboration = int(policy.get("min_corroboration", 1) or 1)
    waiver_classes = _classes_from(policy.get("corroboration_waiver_classes"))
    now = datetime.now(UTC)

    sources = await store.list_documents(ctx.uid, ctx.run_id, F.SOURCES_SUBCOLLECTION)
    by_source = {str(row.get("source_id") or row.get("doc_id")): row for row in sources}

    # Group by the normalized (subject, attribute). Both were normalized by the model at
    # extraction time, which is the ONLY reason "starts at $20/mo" and "twenty dollars a
    # month" land in the same group. String munging would not do it.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for source_id, row in by_source.items():
        source_class_raw = str(row.get("source_class") or SourceClass.UNKNOWN.value)
        try:
            source_class = SourceClass(source_class_raw)
        except ValueError:
            source_class = SourceClass.UNKNOWN
        published = _parse_dt(str(row.get("published_at") or ""))
        for candidate in row.get("candidate_claims") or []:
            subject = str(candidate.get("subject") or "").strip()
            attribute = str(candidate.get("attribute") or "").strip()
            value = str(candidate.get("value_normalized") or "").strip()
            excerpt = str(candidate.get("excerpt") or "").strip()
            # No span, no claim. Enforced again here because this is the last point
            # before a claim becomes durable.
            if not (subject and attribute and value and excerpt):
                continue
            source_url = str(row.get("final_url") or row.get("url") or "")
            groups.setdefault((subject.casefold(), attribute.casefold()), []).append({
                "source_id": source_id,
                "url": source_url,
                "domain": str(row.get("domain") or ""),
                # eTLD+1. Computed from the URL that was actually READ (final_url after
                # redirects), so a redirect into a sibling subdomain cannot manufacture a
                # second independent publisher.
                "publisher": independence_key(source_url or str(row.get("domain") or "")),
                "source_class": source_class,
                "published": published,
                "published_at": str(row.get("published_at") or ""),
                "retrieved_at": str(row.get("retrieved_at") or now.isoformat()),
                "excerpt": excerpt,
                "subject": subject,
                "attribute": attribute,
                "value_normalized": value,
                "text": str(candidate.get("text") or ""),
                "sub_question_id": str(candidate.get("sub_question_id") or ""),
                "eligible_sub_question_ids": list(
                    candidate.get("eligible_sub_question_ids") or ()
                ),
                "claim_kind": str(candidate.get("claim_kind") or "fact"),
                "scope_qualifiers": list(candidate.get("scope_qualifiers") or ()),
            })

    provider = get_model_provider()
    meter = StageMeter()
    adjudications = 0
    claims: dict[str, dict[str, Any]] = {}
    disputed_pairs: list[tuple[str, str]] = []

    # Adjudicate only as many disagreements as the ledger actually paid for, compared
    # numerically against every unit an adjudication consumes.
    #
    # This stage previously ran up to ADJUDICATION_MAX calls without reading ctx.grant at
    # all, so a degraded grant was reserved and then ignored: verify made six calls
    # against a grant of two, and the four extra were recorded as overrun after the fact.
    # A budget that is consulted only in the ledger is not a budget the stage respects.
    adjudication_budget = min(
        ADJUDICATION_MAX,
        int(ctx.grant.get(F.UNIT_MODEL_CALLS) or 0) // RESEARCH_ATTEMPT_BUDGET,
        int(ctx.grant.get(F.UNIT_MODEL_INPUT_TOKENS) or 0)
        // (VERIFY_INPUT_TOKENS_PER_CALL * RESEARCH_ATTEMPT_BUDGET),
        int(ctx.grant.get(F.UNIT_MODEL_OUTPUT_TOKENS) or 0)
        // (VERIFY_OUTPUT_TOKENS_PER_CALL * RESEARCH_ATTEMPT_BUDGET),
    )
    logger.info(
        "research.verify: adjudication budget",
        {"run_id": ctx.run_id, "ceiling": ADJUDICATION_MAX,
         "granted_calls": int(ctx.grant.get(F.UNIT_MODEL_CALLS) or 0),
         "granted_input_tokens": int(ctx.grant.get(F.UNIT_MODEL_INPUT_TOKENS) or 0),
         "adjudications_allowed": adjudication_budget},
    )

    for (_, _), members in groups.items():
        by_value: dict[str, list[dict[str, Any]]] = {}
        for member in members:
            by_value.setdefault(member["value_normalized"].casefold(), []).append(member)

        verdict = "agreement"
        winning_value = ""
        scopes: list[dict[str, str]] = []
        scope_by_value: dict[str, dict[str, str]] = {}
        if len(by_value) > 1 and adjudications < adjudication_budget:
            if ctx.is_cancelled is not None and await ctx.is_cancelled():
                break
            representative = [group[0] for group in by_value.values()]
            try:
                async with meter_models(
                    meter,
                    run_id=ctx.run_id,
                    stage_kind=ctx.stage_kind,
                    ctx=ctx,
                    reserved_input_tokens_per_attempt=VERIFY_INPUT_TOKENS_PER_CALL,
                    reserved_output_tokens_per_attempt=VERIFY_OUTPUT_TOKENS_PER_CALL,
                ):
                    result = await provider.balanced(
                        adjudicate_user_prompt(
                            representative[0]["subject"],
                            representative[0]["attribute"],
                            [
                                {
                                    "value_normalized": item["value_normalized"],
                                    "published_at": item["published_at"],
                                    "source_class": item["source_class"].value,
                                    "domain": item["domain"],
                                    "excerpt": item["excerpt"],
                                    "scope_qualifiers": item["scope_qualifiers"],
                                }
                                for item in representative
                            ],
                        ),
                        system=ADJUDICATE_SYSTEM,
                        response_model=Adjudication,
                        max_output_tokens=VERIFY_OUTPUT_TOKENS_PER_CALL,
                    )
                adjudications += 1
                if isinstance(result, Adjudication):
                    parsed = cast(Adjudication, result)
                    verdict, winning_value, scopes, violation = _enforce_invariants(
                        parsed, representative
                    )
                    if verdict == AdjudicationVerdict.DIFFERENT_SCOPE:
                        scope_by_value = dict(zip(by_value, scopes, strict=True))
                    if violation:
                        # The enum was well-formed and the CLAIM behind it was not. Every
                        # verdict except contradiction changes what the user is shown -
                        # agreement merges two sources into one number, different_scope
                        # keeps both as qualified facts, superseded_by_recency marks one
                        # stale - and each of those was being taken on the model's word.
                        # Failing an invariant falls back to contradiction, which is the
                        # only verdict that asserts nothing and hides nothing.
                        logger.warn(
                            "research.verify: adjudication verdict failed its invariant",
                            {"run_id": ctx.run_id, "claimed_verdict": str(parsed.verdict),
                             "violation": violation,
                             "error_code": "research_adjudication_invariant_failed"},
                        )
            except Exception as exc:
                # An adjudicator that fails leaves the values DISPUTED, which is the
                # conservative direction: unresolved disagreement stays visible rather
                # than one value being silently promoted.
                logger.warn(
                    "research.verify: adjudication failed",
                    {"run_id": ctx.run_id, "error": str(exc),
                     "error_code": "research_adjudication_failed"},
                )
                verdict = "contradiction"
        elif len(by_value) > 1:
            verdict = "contradiction"

        merged_values = (
            [max(by_value.items(), key=lambda kv: len(kv[1]))[0]]
            if verdict == "agreement" and len(by_value) > 1
            else list(by_value)
        )
        group_claim_ids: list[str] = []

        for value_key in merged_values:
            members_for_value = (
                [item for group in by_value.values() for item in group]
                if verdict == "agreement"
                else by_value[value_key]
            )
            first = members_for_value[0]
            claim_id = _claim_id(first["subject"], first["attribute"], value_key)

            evidence: list[dict[str, Any]] = []
            publishers: set[str] = set()
            corroborating: list[dict[str, Any]] = []
            newest: datetime | None = None
            for member in members_for_value[:6]:
                evidence.append({
                    "source_id": member["source_id"],
                    "url": member["url"],
                    "excerpt": member["excerpt"],
                    "retrieved_at": member["retrieved_at"],
                    "published_at": member["published_at"],
                    "source_class": member["source_class"].value,
                })
                # Aggregators and quarantined pages are excluded from corroboration
                # counting outright. Everything else counts, including a discouraged
                # class: discouraged deprioritises, it does not disqualify.
                if member["source_class"] not in NON_CORROBORATING_CLASSES:
                    corroborating.append(member)
                    # By PUBLISHER, not by hostname. Counting hostnames let three
                    # subdomains of one site corroborate each other.
                    key = member["publisher"] or member["domain"]
                    if key:
                        publishers.add(key)
                if member["published"] and (newest is None or member["published"] > newest):
                    newest = member["published"]

            if not evidence:
                continue

            support_domains = len(publishers)
            shortfalls = _policy_shortfalls(corroborating, policy)
            # "1 if DOCS_REFERENCE else 2", expressed as data. One source of a waiver
            # class stands on its own: official documentation restating its own product's
            # behaviour is not made truer by a second site repeating it.
            waived = bool(
                waiver_classes
                and any(m["source_class"] in waiver_classes for m in corroborating)
            )
            required = 1 if waived else max(min_corroboration, 1)

            if verdict == "contradiction":
                confidence = "disputed"
            elif support_domains == 0:
                confidence = "unverified"
            elif support_domains >= required and not shortfalls:
                confidence = "corroborated"
            else:
                # Enough publishers but the wrong KIND of publisher lands here too. A
                # claim that needs a regulator and has two blogs is not corroborated, and
                # synthesis refuses to let single_source answer a must-answer under a
                # policy that demanded more.
                confidence = "single_source"

            freshness = _freshness(newest, max_age_days, now)
            superseded_by = ""
            if verdict == "superseded_by_recency" and winning_value:
                if value_key != winning_value.casefold():
                    # The older claim STAYS VISIBLE with its date. Superseding is a label,
                    # not a deletion.
                    freshness = "stale"
                    superseded_by = _claim_id(
                        first["subject"], first["attribute"], winning_value.casefold()
                    )

            claims[claim_id] = {
                "claim_id": claim_id,
                "sub_question_id": first["sub_question_id"],
                "eligible_sub_question_ids": sorted({
                    str(sub_id)
                    for member in members_for_value
                    for sub_id in (member.get("eligible_sub_question_ids") or ())
                    if str(sub_id)
                }),
                "claim_kind": first["claim_kind"],
                "subject": first["subject"],
                "attribute": first["attribute"],
                "value_normalized": first["value_normalized"],
                "text": first["text"],
                "evidence": evidence,
                "support_domains": support_domains,
                "confidence": confidence,
                "freshness": freshness,
                "as_of": newest.isoformat() if newest else "",
                "contradicts": [],
                "superseded_by": superseded_by,
                "admitted_by": str(policy.get("policy_id") or ""),
                # Persist only code-verified, excerpt-grounded scope metadata. The
                # adjudicator has no free-prose scope field to persist or render.
                "scope": scope_by_value.get(value_key),
                # Which source requirements this evidence failed, so synthesis can name
                # the gap precisely ("no regulator source") instead of the generic
                # "not enough sources".
                "policy_shortfalls": shortfalls,
                "corroboration_waived": waived,
            }
            group_claim_ids.append(claim_id)

        if verdict == "contradiction" and len(group_claim_ids) > 1:
            for claim_id in group_claim_ids:
                disputed_pairs.append((claim_id, ""))
                claims[claim_id]["contradicts"] = [
                    other for other in group_claim_ids if other != claim_id
                ][:6]

    bounded = dict(list(claims.items())[:CLAIMS_MAX])
    evidence_as_of = ""
    for claim in bounded.values():
        if claim["as_of"] and claim["as_of"] > evidence_as_of:
            evidence_as_of = claim["as_of"]

    return StageResult(
        kind=StageResultKind.DONE,
        next_state=F.STATE_SYNTHESIZING,
        next_jobs=(NextJob(stage_kind=F.STAGE_SYNTHESIZE, wave=ctx.wave),),
        documents={F.CLAIMS_SUBCOLLECTION: bounded},
        run_updates={
            F.CLAIM_COUNT: len(bounded),
            F.SOURCE_COUNT: len(by_source),
            F.EVIDENCE_AS_OF: evidence_as_of,
        },
        stage_outputs={
            "groups": len(groups),
            "claims": len(bounded),
            "adjudications": adjudications,
            "disputed": len([c for c in bounded.values() if c["confidence"] == "disputed"]),
            "policy_blocked": len(
                [c for c in bounded.values() if c["policy_shortfalls"]]
            ),
            "cost_incomplete": meter.cost_incomplete,
        },
        # Measured, not counted. This stage may make up to ADJUDICATION_MAX calls, and
        # reporting only the attempt count left the token meters permanently at zero.
        actuals=meter.as_actuals(),
        cost_microusd=meter.cost_microusd,
        cost_known=not meter.cost_incomplete,
    )
