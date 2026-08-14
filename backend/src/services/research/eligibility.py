"""Conservative claim-to-question admission.

The extractor may suggest a sub-question id, but that string is never coverage. This
module makes the narrower guarantee the product needs: a claim satisfies a must-answer
only after code positively matches the admitted question and its named entities against
the claim's subject, attribute, normalized value, and authoritative excerpt.

This is deliberately not general entailment. False negatives become visible supplemental
evidence and a deterministic gap. A false positive would hide a required gap, so every
uncertain case is rejected.
"""

from __future__ import annotations

import re
from typing import Any

from .models import EntityBindingStatus, ScopeDimension

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "a", "about", "an", "and", "are", "as", "at", "be", "by", "can", "could",
    "did", "do", "does", "for", "from", "has", "have", "how", "in", "is", "it",
    "its", "of", "on", "or", "that", "the", "their", "this", "to", "was", "were",
    "what", "when", "where", "which", "who", "why", "with", "would",
})
_SCOPE_DIMENSION_CUES = {
    ScopeDimension.PLAN_TIER: ("plan", "tier", "package", "subscription"),
    ScopeDimension.JURISDICTION: (
        "jurisdiction", "law", "legal", "regulation", "regulatory", "court",
    ),
    ScopeDimension.REGION: (
        "region", "country", "market", "location", "geography", "geographic",
        "territory", "state", "province", "city",
    ),
    ScopeDimension.TIME_PERIOD: (
        "year", "month", "quarter", "week", "day", "period", "annual",
        "monthly", "quarterly",
    ),
    ScopeDimension.UNIT: (
        "unit", "per", "usd", "dollar", "percent", "percentage", "kilogram",
        "meter", "mile", "byte",
    ),
    ScopeDimension.POPULATION: (
        "population", "people", "users", "customers", "employees", "adults",
        "children", "participants", "respondents",
    ),
    ScopeDimension.PRODUCT_VARIANT: (
        "variant", "model", "version", "size", "color", "configuration",
    ),
}


def _tokens(value: str) -> set[str]:
    return {
        token for token in _WORD_RE.findall((value or "").casefold())
        if len(token) > 1 and token not in _STOPWORDS
    }


def _ordered_tokens(value: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall((value or "").casefold()))


def _contains_phrase(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(
        haystack[index : index + width] == needle
        for index in range(len(haystack) - width + 1)
    )


def validate_scope_qualifier(
    *,
    dimension: ScopeDimension,
    value: str,
    evidence_excerpt: str,
    authoritative_excerpt: str,
) -> dict[str, str] | None:
    """Return bounded scope metadata only when the excerpt proves value and dimension."""
    authoritative_tokens = _ordered_tokens(authoritative_excerpt)
    evidence_tokens = _ordered_tokens(evidence_excerpt)
    value_tokens = _ordered_tokens(value)
    if not authoritative_tokens or not evidence_tokens or not value_tokens:
        return None
    if not _contains_phrase(authoritative_tokens, evidence_tokens):
        return None
    # Token-sequence containment is load bearing. Short values such as US and IN must
    # never match inside Business, Premium, or any other unrelated token.
    if not _contains_phrase(evidence_tokens, value_tokens):
        return None
    supported_dimensions = {
        candidate_dimension
        for candidate_dimension, cues in _SCOPE_DIMENSION_CUES.items()
        if any(cue in evidence_tokens for cue in cues)
    }
    # The page must identify one unambiguous dimension. The extractor cannot relabel a
    # plan tier as a region, and ambiguous evidence fails closed to contradiction.
    if supported_dimensions != {dimension}:
        return None
    return {"dimension": dimension.value, "value": " ".join(value.split())}


def entity_binding_status(
    *, sub_question_id: str, plan: dict[str, Any], excerpt: str = ""
) -> EntityBindingStatus:
    """Re-derive a sub-question's binding state from the admitted plan and excerpt."""
    questions = {
        str(item.get("sub_question_id") or ""): item
        for item in (plan.get("sub_questions") or ())
        if str(item.get("sub_question_id") or "")
    }
    question = questions.get(sub_question_id)
    if not question:
        return EntityBindingStatus.MISMATCHED

    bindings = [
        str(item.get("entity") or "").strip()
        for item in (question.get("entity_bindings") or ())
        if isinstance(item, dict) and str(item.get("entity") or "").strip()
    ]
    if not bindings:
        return EntityBindingStatus.MISSING

    entity_catalog: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for entity in (plan.get("entities") or ()):
        phrase = _ordered_tokens(str(entity))
        if phrase:
            entity_catalog.setdefault(phrase, []).append(phrase)
    matches = [entity_catalog.get(_ordered_tokens(binding), []) for binding in bindings]
    if any(not match for match in matches):
        return EntityBindingStatus.MISMATCHED
    if any(len(match) != 1 for match in matches):
        return EntityBindingStatus.AMBIGUOUS
    resolved = [match[0] for match in matches]
    if len(set(resolved)) != len(resolved):
        return EntityBindingStatus.AMBIGUOUS
    if excerpt:
        excerpt_ordered = _ordered_tokens(excerpt)
        if not all(_contains_phrase(excerpt_ordered, entity) for entity in resolved):
            return EntityBindingStatus.MISMATCHED
    return EntityBindingStatus.VALID


def eligible_sub_question_ids(
    *,
    sub_question_id: str,
    plan: dict[str, Any],
    subject: str,
    attribute: str,
    value: str,
    excerpt: str,
) -> tuple[str, ...]:
    """Return the suggested id only when this exact question is positively admitted."""
    questions = {
        str(item.get("sub_question_id") or ""): item
        for item in (plan.get("sub_questions") or ())
        if str(item.get("sub_question_id") or "")
    }
    question_row = questions.get(sub_question_id)
    question = str(question_row.get("text") or "") if question_row else ""
    required = (subject.strip(), attribute.strip(), value.strip(), excerpt.strip())
    if not question or not all(required):
        return ()
    if entity_binding_status(
        sub_question_id=sub_question_id, plan=plan, excerpt=excerpt
    ) is not EntityBindingStatus.VALID:
        return ()

    question_tokens = _tokens(question)
    subject_tokens = _tokens(subject)
    attribute_tokens = _tokens(attribute)
    value_tokens = _tokens(value)
    excerpt_tokens = _tokens(excerpt)
    if not question_tokens or not excerpt_tokens:
        return ()

    # Subject is model metadata, so its label cannot prove itself. At least one
    # meaningful subject token must occur in both the admitted question and quote.
    if not subject_tokens or not (subject_tokens & question_tokens & excerpt_tokens):
        return ()

    claim_tokens = subject_tokens | attribute_tokens | value_tokens | excerpt_tokens
    if len(question_tokens & claim_tokens) < 2:
        return ()
    if not ((attribute_tokens | value_tokens) & excerpt_tokens):
        return ()

    return (sub_question_id,)
