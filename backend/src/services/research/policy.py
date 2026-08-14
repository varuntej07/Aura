"""Composition of source policies. One pure function, no I/O and no model call.

A run is rarely one row. "Compare treatments for X" is clinical AND scientific; "should
we buy this vendor" is product comparison AND pricing. Rather than a code path per
scenario, the classifier names up to three rows and this module intersects them by
taking the STRICTEST value of every field. Adding a fifteenth scenario is one row of
data in ``policy_table.py`` and no change here.

Strictest wins, field by field:

    recency.max_age_days     min of the bounded values (None = unbounded, ignored)
    recency.hard             any
    min_corroboration        max
    risk_class               max by RISK_ORDER
    primary_source_required  any
    independence_required    any
    requires_disclaimer      any
    partial_over_guess       any
    disclaimer_keys          union, ordered
    required class groups    concatenation (a conjunction: every group must hold)
    corroboration waivers    INTERSECTION, because a waiver RELAXES the rule
    preferred classes        union
    acceptable               union, minus anything already preferred
    discouraged              union, minus anything any row preferred
    output_sections          ordered union, first-named profile first

Three data-driven guards keep a novel topic safe, in the order they apply:

1. An unknown profile id coerces to ``generic_open_question``. It can never raise and
   can never silently select row 0.
2. Confidence below CONFIDENCE_FLOOR ADDS the generic row rather than substituting it,
   so composition takes the stricter of the model's guess and the conservative default.
3. Non-empty risk flags floor risk at medium and force a disclaimer.

``generic_open_question`` is deliberately stricter than several named rows, so an
unrecognised question gets MORE caution, not less.

The composed result is persisted on the run as ``effective_policy``. That is the audit
trail answering "why did this brief demand three sources" without re-running anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .policy_table import (
    GENERIC_POLICY_ID,
    POLICY_TABLE,
    RISK_ORDER,
    RecencyRule,
    RiskClass,
    SourceClass,
    SourcePolicy,
)

# Below this the classifier is guessing, so the conservative row is added alongside its
# guess. Not a rejection threshold: the guess still contributes its own strictness.
CONFIDENCE_FLOOR = 0.55

# A composition is capped at three rows, matching ResearchPlan.profile_ids. Beyond
# three the composed policy is so strict that nothing satisfies it and every run
# degrades to a gap list.
MAX_PROFILES = 3


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    """Union that preserves first-seen order. dict.fromkeys, not set, on purpose:
    output_sections is rendered in order and a set would scramble the brief."""
    return tuple(dict.fromkeys(values))


def _ordered_unique_classes(values: Iterable[SourceClass]) -> tuple[SourceClass, ...]:
    return tuple(dict.fromkeys(values))


def resolve_profile_ids(
    profile_ids: Sequence[str], *, confidence: float
) -> tuple[str, ...]:
    """Apply guards 1 and 2 and return the rows composition will actually use.

    Separate from compose() because admission and telemetry both want to record which
    rows were used, including a generic row the classifier did not ask for.
    """
    resolved: list[str] = []
    for profile_id in profile_ids:
        # Guard 1: an unrecognised id becomes the conservative row, never a crash and
        # never a silent row 0.
        resolved.append(profile_id if profile_id in POLICY_TABLE else GENERIC_POLICY_ID)

    # Guard 2: a low-confidence classification ADDS the generic row. Substituting it
    # would throw away the guess; adding it means composition takes the stricter of the
    # two, which is the only safe direction.
    if confidence < CONFIDENCE_FLOOR:
        resolved.append(GENERIC_POLICY_ID)

    if not resolved:
        resolved.append(GENERIC_POLICY_ID)

    return _ordered_unique(resolved)[:MAX_PROFILES]


def _compose_recency(rows: Sequence[SourcePolicy]) -> RecencyRule:
    bounded = [
        row.recency.max_age_days
        for row in rows
        if row.recency.max_age_days is not None
    ]
    return RecencyRule(
        # An unbounded row (event_timeline) does not loosen a bounded one. Composing it
        # with a 90-day row must still give 90 days.
        max_age_days=min(bounded) if bounded else None,
        hard=any(row.recency.hard for row in rows),
    )


# Every row ends with this section, so a plain ordered union would strand the first
# row's copy in the middle of the brief and render findings after the gap list.
_TRAILING_SECTION = "gaps"


def _compose_sections(rows: Sequence[SourcePolicy]) -> tuple[str, ...]:
    """Ordered union, first-named profile first, with the gap list kept last."""
    sections = _ordered_unique(
        section for row in rows for section in row.output_sections
    )
    body = tuple(section for section in sections if section != _TRAILING_SECTION)
    if len(body) == len(sections):
        return sections
    return (*body, _TRAILING_SECTION)


def compose(
    profile_ids: Sequence[str],
    *,
    confidence: float = 1.0,
    risk_flags: Sequence[str] = (),
) -> SourcePolicy:
    """Build the effective policy for one run. Pure, total, and never raises.

    Any input is answerable: an empty list, an unknown id, or a garbage confidence all
    produce a valid policy at least as strict as ``generic_open_question``.
    """
    resolved_ids = resolve_profile_ids(profile_ids, confidence=confidence)
    rows = [POLICY_TABLE[profile_id] for profile_id in resolved_ids]

    if len(rows) == 1 and not risk_flags:
        # Nothing to intersect and no flags to apply. Returning the row itself keeps
        # the single-profile case byte-identical to the table, which makes the stored
        # effective_policy directly comparable against it.
        return rows[0]

    preferred = _ordered_unique_classes(
        source_class for row in rows for source_class in row.preferred_source_classes
    )
    acceptable = tuple(
        source_class
        for source_class in _ordered_unique_classes(
            item for row in rows for item in row.acceptable_source_classes
        )
        if source_class not in preferred
    )
    # A class one row discourages but another prefers is NOT discouraged: the run needs
    # it. This is why discouraged is computed against the union of preferred rather than
    # per row.
    discouraged = tuple(
        source_class
        for source_class in _ordered_unique_classes(
            item for row in rows for item in row.discouraged_source_classes
        )
        if source_class not in preferred and source_class not in acceptable
    )

    # Conjunction: every group from every row must still be satisfied. Identical groups
    # from two rows collapse to one.
    required_groups = tuple(
        dict.fromkeys(group for row in rows for group in row.required_source_class_groups)
    )

    # A waiver lets ONE source satisfy corroboration, so it relaxes the policy and must
    # intersect, not union. A row with no waiver zeroes the composed waiver, which is
    # the correct strict outcome.
    waivers: tuple[SourceClass, ...] = rows[0].corroboration_waiver_classes
    for row in rows[1:]:
        waivers = tuple(
            source_class
            for source_class in waivers
            if source_class in row.corroboration_waiver_classes
        )

    risk = max((row.risk_class for row in rows), key=lambda value: RISK_ORDER[value])
    requires_disclaimer = any(row.requires_disclaimer for row in rows)
    disclaimer_keys = _ordered_unique(key for row in rows for key in row.disclaimer_keys)

    # Guard 3: a flagged run is never low risk and never undisclaimed, whatever the
    # composed rows said.
    if risk_flags:
        if RISK_ORDER[risk] < RISK_ORDER[RiskClass.MEDIUM]:
            risk = RiskClass.MEDIUM
        requires_disclaimer = True
        disclaimer_keys = _ordered_unique((*disclaimer_keys, "general_information"))

    composed_id = "+".join(resolved_ids)
    return SourcePolicy(
        policy_id=composed_id,
        label=" + ".join(row.label for row in rows),
        # The composed row is never shown to the classifier (CLASSIFIER_CHOICES is built
        # from the table, not from compositions), so this describes the composition for
        # a human reading the audit trail.
        intent_summary=f"Composed from {', '.join(resolved_ids)}.",
        preferred_source_classes=preferred,
        acceptable_source_classes=acceptable,
        discouraged_source_classes=discouraged,
        required_source_class_groups=required_groups,
        corroboration_waiver_classes=waivers,
        recency=_compose_recency(rows),
        min_corroboration=max(row.min_corroboration for row in rows),
        primary_source_required=any(row.primary_source_required for row in rows),
        independence_required=any(row.independence_required for row in rows),
        risk_class=risk,
        requires_disclaimer=requires_disclaimer,
        disclaimer_keys=disclaimer_keys,
        partial_over_guess=any(row.partial_over_guess for row in rows),
        output_sections=_compose_sections(rows),
        query_shape_hints=_ordered_unique(
            hint for row in rows for hint in row.query_shape_hints
        ),
    )
