"""The declarative source policy table. Pure data, no imports beyond pydantic.

There are no topic lists, no hostname allowlists, and no regex classification anywhere
in this module, deliberately. Two ideas make that possible:

1. ``SourceClass`` is a closed vocabulary of EPISTEMIC ROLES, not topics. It says what
   part a page plays in an argument (a regulator, a vendor talking about itself, a
   peer-reviewed study), never what the page is about. A domain is mapped to one of
   these by a cached model call, so the mapping is learned and refreshed rather than
   listed here.

2. A row is keyed by the SHAPE of a question, and ``intent_summary`` is the only thing
   the classifier ever matches on. It describes that shape in natural language, never a
   subject area, so an unfamiliar topic still lands somewhere sensible.

The 14 rows compose. Every target scenario is an intersection of two or three of them
rather than a code path, which is why a fifteenth scenario is one row of data and no
branching. ``policy.compose`` takes the strictest value of every field.

Two deliberate divergences from the shape sketched in
``BACKGROUND_RESEARCH_AGENT_ARCHITECTURE.md`` section 6.2, both because the sketch could
not represent its own table:

- ``disclaimer_keys`` is plural. Section 6.3 composes disclaimers by UNION, so the
  composed policy has to be able to hold more than one; the patient-treatment scenario
  carries both the clinical and the scientific disclaimer.
- ``required_source_classes`` and ``corroboration_waiver_classes`` exist because six
  rows in the table carry a class constraint the listed fields had no room for
  ("2, at least one REGULATOR_GOV"; "1 if DOCS_REFERENCE else 2"). Encoding those as
  data keeps the verify stage free of per-policy branching.

``independence_required`` is not in the section 6.2 table at all. It is set from the
use-case matrix in section 0.4, which names independent sourcing as the required
observable behavior for exactly the rows marked below.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceClass(StrEnum):
    """What ROLE a page plays in an argument. Never a topic, never a domain name."""

    PRIMARY_ORG = "primary_org"                  # the subject's own site, filings, press
    REGULATOR_GOV = "regulator_gov"              # government, regulator, court, statute
    STANDARDS_BODY = "standards_body"            # ISO, W3C, IETF, clinical guideline body
    PEER_REVIEWED = "peer_reviewed"              # journal article
    PREPRINT = "preprint"
    SYSTEMATIC_REVIEW = "systematic_review"      # meta-analysis, evidence synthesis
    ESTABLISHED_NEWS = "established_news"
    TRADE_PRESS = "trade_press"
    INDEPENDENT_REVIEW = "independent_review"    # editorially independent testing
    VENDOR_MARKETING = "vendor_marketing"        # a vendor on itself, incl. comparison pages
    DOCS_REFERENCE = "docs_reference"            # official product or API documentation
    REPO_CHANGELOG = "repo_changelog"            # source repo, release notes, issues
    MARKETPLACE_LISTING = "marketplace_listing"  # store, listing, aggregated price page
    COMMUNITY_FORUM = "community_forum"          # SO, Reddit, HN, user forums
    PERSONAL_BLOG = "personal_blog"
    AGGREGATOR = "aggregator"                    # content farms, SEO listicles, mirrors
    UNTRUSTED = "untrusted"                      # forced by the injection quarantine
    UNKNOWN = "unknown"                          # unproven publisher identity


# These classes are excluded from corroboration counting outright. Everything else in
# `discouraged_source_classes` is merely deprioritised in the read queue: a discouraged
# class still counts as evidence, because banning sources by class would quietly turn a
# preference into a gate.
NON_CORROBORATING_CLASSES: tuple[SourceClass, ...] = (
    SourceClass.UNKNOWN,
    SourceClass.AGGREGATOR,
    SourceClass.UNTRUSTED,
)

# What satisfies ``primary_source_required``: the record itself rather than someone's
# account of it. A company's own filing, a regulator's text, a standard, the official
# documentation, the repository. "Reported by a reputable outlet" is corroboration, but
# it is not the primary record, which is the whole distinction that flag draws.
PRIMARY_SOURCE_CLASSES: tuple[SourceClass, ...] = (
    SourceClass.PRIMARY_ORG,
    SourceClass.REGULATOR_GOV,
    SourceClass.STANDARDS_BODY,
    SourceClass.DOCS_REFERENCE,
    SourceClass.REPO_CHANGELOG,
    SourceClass.PEER_REVIEWED,
)

# What satisfies ``independence_required``: a source with no stake in the answer. The
# exclusions are the parties being described (the subject's own site, a vendor on itself,
# a listing that exists to sell the thing) plus the classes that never corroborate at
# all. A vendor's comparison page is evidence about THAT VENDOR'S CLAIMS, not about its
# competitors, which is exactly why product_comparison sets this flag.
NON_INDEPENDENT_CLASSES: tuple[SourceClass, ...] = (
    SourceClass.UNKNOWN,
    SourceClass.PRIMARY_ORG,
    SourceClass.VENDOR_MARKETING,
    SourceClass.MARKETPLACE_LISTING,
    SourceClass.AGGREGATOR,
    SourceClass.UNTRUSTED,
)

# Rendered onto the brief when the composed policy asks for them. The text is
# deliberately plain and short: a disclaimer nobody reads protects nobody.
DISCLAIMER_TEXT: dict[str, str] = {
    "not_medical_advice": (
        "This is general information gathered from published sources, not medical "
        "advice. Talk to a qualified clinician about your own situation."
    ),
    "not_legal_advice": (
        "This is general information gathered from published sources, not legal advice. "
        "Rules vary by jurisdiction and change; check with a qualified lawyer."
    ),
    "not_financial_advice": (
        "This is general information gathered from published sources, not financial "
        "advice. Nothing here is a recommendation to buy or sell anything."
    ),
    "confirm_before_travel": (
        "Travel details change often and without notice. Confirm directly with the "
        "operator or venue before you rely on anything here."
    ),
    "pricing_may_change": (
        "Prices and plans change frequently. Check the seller's own page for the "
        "current figure before deciding."
    ),
    "research_not_advice": (
        "This summarises published research. Findings can be preliminary, contested, or "
        "superseded, and are not advice for any individual case."
    ),
    "general_information": (
        "This is general information assembled from public sources. Check anything you "
        "intend to act on against the original source."
    ),
}


class RiskClass(StrEnum):
    """How much damage a wrong answer does. Ordered by RISK_ORDER, not alphabetically."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# A StrEnum has no meaningful ordering of its own, and composition needs a real max().
# Kept as data next to the enum so the two can never drift.
RISK_ORDER: dict[RiskClass, int] = {
    RiskClass.LOW: 0,
    RiskClass.MEDIUM: 1,
    RiskClass.HIGH: 2,
}


class RecencyRule(BaseModel):
    """How old a source may be before it stops being evidence about the present.

    ``hard`` is the load-bearing bit. Under a soft rule a dated claim can still assert
    present-tense fact with an "as of" qualifier. Under a hard rule it cannot: only a
    `current` claim may answer a must-answer sub-question, and a must-answer with no
    current source becomes a gap. That is what stops "the lift was step-free in 2019"
    from being served as travel advice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # None means unbounded: an event timeline about 1962 is not stale.
    max_age_days: int | None = Field(default=None, ge=1)
    hard: bool = False


class SourcePolicy(BaseModel):
    """One row, or the composition of several. Both are the same type on purpose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    label: str
    # The ONLY field the classifier matches on. Describes the shape of the question,
    # never its subject.
    intent_summary: str

    preferred_source_classes: tuple[SourceClass, ...] = ()
    acceptable_source_classes: tuple[SourceClass, ...] = ()
    discouraged_source_classes: tuple[SourceClass, ...] = ()
    # A conjunction of disjunctions: EVERY group must be satisfied by at least one
    # corroborating source. Nested rather than flat because composition concatenates
    # groups, and a flat union would be WEAKER than its inputs. Composing clinical
    # (STANDARDS_BODY) with scientific (PEER_REVIEWED or SYSTEMATIC_REVIEW) as one flat
    # set would accept a lone journal article and satisfy neither original rule.
    required_source_class_groups: tuple[tuple[SourceClass, ...], ...] = ()
    # A single source of one of these classes satisfies min_corroboration on its own.
    # This is how "1 if DOCS_REFERENCE/REPO_CHANGELOG else 2" is expressed as data.
    corroboration_waiver_classes: tuple[SourceClass, ...] = ()

    recency: RecencyRule = RecencyRule()
    # Distinct DOMAINS, not distinct URLs. Three pages on one site are one source.
    min_corroboration: int = Field(default=2, ge=1, le=5)
    primary_source_required: bool = False
    independence_required: bool = False

    risk_class: RiskClass = RiskClass.LOW
    requires_disclaimer: bool = False
    disclaimer_keys: tuple[str, ...] = ()
    # True on every row: an unsupported must-answer becomes a GAP, never an inference.
    partial_over_guess: bool = True

    output_sections: tuple[str, ...] = ()
    # Hints for the query GENERATOR. Not patterns anything is matched against.
    query_shape_hints: tuple[str, ...] = ()


GENERIC_POLICY_ID = "generic_open_question"


POLICY_TABLE: dict[str, SourcePolicy] = {
    "market_landscape": SourcePolicy(
        policy_id="market_landscape",
        label="Market landscape",
        intent_summary=(
            "Who the players in a space are and how they differ, when the answer is a "
            "survey of a field rather than a fact about one named thing."
        ),
        preferred_source_classes=(
            SourceClass.TRADE_PRESS, SourceClass.INDEPENDENT_REVIEW,
            SourceClass.ESTABLISHED_NEWS, SourceClass.PRIMARY_ORG,
        ),
        acceptable_source_classes=(SourceClass.DOCS_REFERENCE, SourceClass.PERSONAL_BLOG),
        discouraged_source_classes=(SourceClass.AGGREGATOR, SourceClass.MARKETPLACE_LISTING),
        recency=RecencyRule(max_age_days=365, hard=False),
        min_corroboration=2,
        independence_required=True,
        risk_class=RiskClass.LOW,
        output_sections=("landscape", "players", "differentiators", "gaps"),
        query_shape_hints=("alternatives to", "vendors in", "market overview"),
    ),
    "entity_dossier": SourcePolicy(
        policy_id="entity_dossier",
        label="Entity dossier",
        intent_summary=(
            "What is currently true about one specific named organisation or product, "
            "assembled from that entity's own record plus outside reporting."
        ),
        preferred_source_classes=(
            SourceClass.PRIMARY_ORG, SourceClass.ESTABLISHED_NEWS, SourceClass.TRADE_PRESS,
        ),
        acceptable_source_classes=(SourceClass.REGULATOR_GOV, SourceClass.DOCS_REFERENCE),
        discouraged_source_classes=(SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG),
        recency=RecencyRule(max_age_days=180, hard=False),
        min_corroboration=2,
        primary_source_required=True,
        risk_class=RiskClass.LOW,
        output_sections=("overview", "recent_developments", "sources", "gaps"),
        query_shape_hints=("official site", "recent news about", "about page"),
    ),
    "product_comparison": SourcePolicy(
        policy_id="product_comparison",
        label="Product comparison",
        intent_summary=(
            "Which of several named options fits a stated need, where the answer is a "
            "per-criterion contrast rather than a single fact."
        ),
        preferred_source_classes=(
            SourceClass.PRIMARY_ORG, SourceClass.INDEPENDENT_REVIEW,
            SourceClass.DOCS_REFERENCE,
        ),
        acceptable_source_classes=(SourceClass.TRADE_PRESS, SourceClass.COMMUNITY_FORUM),
        # Not banned, only deprioritised and separated: a vendor's own comparison page
        # is evidence about that vendor's claims, not about its competitors.
        discouraged_source_classes=(SourceClass.VENDOR_MARKETING, SourceClass.AGGREGATOR),
        recency=RecencyRule(max_age_days=180, hard=False),
        min_corroboration=2,
        primary_source_required=True,
        independence_required=True,
        risk_class=RiskClass.MEDIUM,
        output_sections=("comparison_table", "tradeoffs", "vendor_claims", "gaps"),
        query_shape_hints=("versus", "comparison", "which is better for"),
    ),
    "pricing_and_terms": SourcePolicy(
        policy_id="pricing_and_terms",
        label="Pricing and terms",
        intent_summary=(
            "What something costs or what a contract commits you to. The seller's own "
            "current page is the only authority; everything else is hearsay about it."
        ),
        preferred_source_classes=(SourceClass.PRIMARY_ORG,),
        acceptable_source_classes=(SourceClass.MARKETPLACE_LISTING, SourceClass.DOCS_REFERENCE),
        discouraged_source_classes=(
            SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG, SourceClass.COMMUNITY_FORUM,
        ),
        required_source_class_groups=((SourceClass.PRIMARY_ORG,),),
        # 1 rather than 2: a second domain quoting the price adds nothing the vendor's
        # own page does not already settle, and usually adds a stale number.
        recency=RecencyRule(max_age_days=90, hard=True),
        min_corroboration=1,
        primary_source_required=True,
        risk_class=RiskClass.MEDIUM,
        requires_disclaimer=True,
        disclaimer_keys=("pricing_may_change",),
        output_sections=("pricing", "terms", "as_of", "gaps"),
        query_shape_hints=("pricing page", "plans and pricing", "terms of service"),
    ),
    "regulatory_compliance": SourcePolicy(
        policy_id="regulatory_compliance",
        label="Regulatory and compliance",
        intent_summary=(
            "What a rule requires of someone in a given place, where the answer changes "
            "by jurisdiction and effective date and a secondary summary is not the rule."
        ),
        preferred_source_classes=(SourceClass.REGULATOR_GOV, SourceClass.STANDARDS_BODY),
        acceptable_source_classes=(SourceClass.TRADE_PRESS, SourceClass.ESTABLISHED_NEWS),
        discouraged_source_classes=(
            SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG,
            SourceClass.VENDOR_MARKETING, SourceClass.COMMUNITY_FORUM,
        ),
        required_source_class_groups=((SourceClass.REGULATOR_GOV,),),
        recency=RecencyRule(max_age_days=365, hard=True),
        min_corroboration=2,
        primary_source_required=True,
        risk_class=RiskClass.HIGH,
        requires_disclaimer=True,
        disclaimer_keys=("not_legal_advice",),
        output_sections=("requirement", "jurisdiction", "effective_dates", "primary_links", "gaps"),
        query_shape_hints=("official regulation", "statute text", "compliance requirements"),
    ),
    "scientific_evidence": SourcePolicy(
        policy_id="scientific_evidence",
        label="Scientific evidence",
        intent_summary=(
            "What the research literature currently supports, where study quality and "
            "disagreement between findings are part of the answer, not noise in it."
        ),
        preferred_source_classes=(
            SourceClass.SYSTEMATIC_REVIEW, SourceClass.PEER_REVIEWED, SourceClass.STANDARDS_BODY,
        ),
        acceptable_source_classes=(SourceClass.PREPRINT, SourceClass.ESTABLISHED_NEWS),
        discouraged_source_classes=(
            SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG, SourceClass.VENDOR_MARKETING,
        ),
        required_source_class_groups=((SourceClass.PEER_REVIEWED, SourceClass.SYSTEMATIC_REVIEW),),
        # 1825 days is long on purpose: a 2019 randomised trial is not stale evidence
        # the way a 2019 price is a stale price.
        recency=RecencyRule(max_age_days=1825, hard=False),
        min_corroboration=2,
        independence_required=True,
        risk_class=RiskClass.MEDIUM,
        requires_disclaimer=True,
        disclaimer_keys=("research_not_advice",),
        output_sections=("findings", "study_quality", "conflicting_evidence", "gaps"),
        query_shape_hints=("systematic review", "randomised trial", "meta-analysis"),
    ),
    "clinical_or_health": SourcePolicy(
        policy_id="clinical_or_health",
        label="Clinical and health",
        intent_summary=(
            "A health question where a wrong answer harms a person. Answerable only as "
            "informational summary of published guidance, never as direction to act."
        ),
        preferred_source_classes=(
            SourceClass.STANDARDS_BODY, SourceClass.SYSTEMATIC_REVIEW,
            SourceClass.PEER_REVIEWED, SourceClass.REGULATOR_GOV,
        ),
        acceptable_source_classes=(SourceClass.PRIMARY_ORG, SourceClass.ESTABLISHED_NEWS),
        discouraged_source_classes=(
            SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG,
            SourceClass.COMMUNITY_FORUM, SourceClass.VENDOR_MARKETING,
        ),
        required_source_class_groups=((SourceClass.STANDARDS_BODY,),),
        recency=RecencyRule(max_age_days=1095, hard=True),
        # The strictest corroboration in the table. Three distinct domains, and a
        # guideline body among them.
        min_corroboration=3,
        risk_class=RiskClass.HIGH,
        requires_disclaimer=True,
        disclaimer_keys=("not_medical_advice",),
        output_sections=("informational_summary", "guideline_positions", "uncertainty", "gaps"),
        query_shape_hints=("clinical guideline", "treatment guidance", "published evidence"),
    ),
    "financial_or_investment": SourcePolicy(
        policy_id="financial_or_investment",
        label="Financial and investment",
        intent_summary=(
            "A money question about a specific entity or instrument, where filings are "
            "the record and the answer stops short of telling anyone what to do."
        ),
        preferred_source_classes=(SourceClass.PRIMARY_ORG, SourceClass.REGULATOR_GOV),
        acceptable_source_classes=(SourceClass.ESTABLISHED_NEWS, SourceClass.TRADE_PRESS),
        discouraged_source_classes=(
            SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG,
            SourceClass.COMMUNITY_FORUM, SourceClass.VENDOR_MARKETING,
        ),
        required_source_class_groups=((SourceClass.PRIMARY_ORG,),),
        recency=RecencyRule(max_age_days=90, hard=True),
        min_corroboration=2,
        primary_source_required=True,
        independence_required=True,
        risk_class=RiskClass.HIGH,
        requires_disclaimer=True,
        disclaimer_keys=("not_financial_advice",),
        output_sections=("reported_figures", "as_of", "disputed_metrics", "gaps"),
        query_shape_hints=("annual report", "filing", "investor relations"),
    ),
    "event_timeline": SourcePolicy(
        policy_id="event_timeline",
        label="Event timeline",
        intent_summary=(
            "What happened and in what order, where each entry needs its own date and "
            "an undated event is not a timeline entry."
        ),
        preferred_source_classes=(
            SourceClass.ESTABLISHED_NEWS, SourceClass.PRIMARY_ORG, SourceClass.REGULATOR_GOV,
        ),
        acceptable_source_classes=(SourceClass.TRADE_PRESS,),
        discouraged_source_classes=(SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG),
        # Unbounded: a timeline of a 1962 event is not stale. Freshness is enforced per
        # dated entry instead, which is what the two-domain rule below is for.
        recency=RecencyRule(max_age_days=None, hard=False),
        min_corroboration=2,
        risk_class=RiskClass.MEDIUM,
        output_sections=("timeline", "uncertain_dates", "sources", "gaps"),
        query_shape_hints=("timeline of", "what happened when", "chronology"),
    ),
    "logistics_and_accessibility": SourcePolicy(
        policy_id="logistics_and_accessibility",
        label="Logistics and accessibility",
        intent_summary=(
            "Whether a place or service will actually work for someone on a given day. "
            "The operator's current page is authority; inference is never acceptable."
        ),
        preferred_source_classes=(SourceClass.PRIMARY_ORG, SourceClass.REGULATOR_GOV),
        acceptable_source_classes=(SourceClass.ESTABLISHED_NEWS, SourceClass.INDEPENDENT_REVIEW),
        discouraged_source_classes=(
            SourceClass.AGGREGATOR, SourceClass.MARKETPLACE_LISTING, SourceClass.PERSONAL_BLOG,
        ),
        required_source_class_groups=((SourceClass.PRIMARY_ORG,),),
        # Hard recency is the single most important property of this row: a lift that
        # worked in 2023 is not evidence about today.
        recency=RecencyRule(max_age_days=365, hard=True),
        min_corroboration=2,
        primary_source_required=True,
        risk_class=RiskClass.MEDIUM,
        requires_disclaimer=True,
        disclaimer_keys=("confirm_before_travel",),
        output_sections=("current_provision", "as_of", "call_ahead", "gaps"),
        query_shape_hints=("accessibility information", "opening hours", "official venue page"),
    ),
    "technical_reference": SourcePolicy(
        policy_id="technical_reference",
        label="Technical reference",
        intent_summary=(
            "How a technical thing behaves at a specific version, where the project's "
            "own documentation and history outrank any number of second-hand accounts."
        ),
        preferred_source_classes=(SourceClass.DOCS_REFERENCE, SourceClass.REPO_CHANGELOG),
        acceptable_source_classes=(
            SourceClass.PRIMARY_ORG, SourceClass.COMMUNITY_FORUM, SourceClass.PERSONAL_BLOG,
        ),
        discouraged_source_classes=(SourceClass.AGGREGATOR, SourceClass.VENDOR_MARKETING),
        # One official doc settles it; anything else needs a second domain.
        corroboration_waiver_classes=(SourceClass.DOCS_REFERENCE, SourceClass.REPO_CHANGELOG),
        recency=RecencyRule(max_age_days=365, hard=False),
        min_corroboration=2,
        primary_source_required=True,
        risk_class=RiskClass.LOW,
        output_sections=("behavior", "version_applicability", "maintenance_status", "gaps"),
        query_shape_hints=("official docs", "changelog", "release notes", "api reference"),
    ),
    "role_or_opportunity": SourcePolicy(
        policy_id="role_or_opportunity",
        label="Role and opportunity",
        intent_summary=(
            "What working somewhere is actually like or what a posting requires, where "
            "a stale listing is worse than no listing."
        ),
        preferred_source_classes=(SourceClass.PRIMARY_ORG, SourceClass.ESTABLISHED_NEWS),
        acceptable_source_classes=(
            SourceClass.TRADE_PRESS, SourceClass.COMMUNITY_FORUM,
            SourceClass.MARKETPLACE_LISTING,
        ),
        discouraged_source_classes=(SourceClass.AGGREGATOR, SourceClass.VENDOR_MARKETING),
        corroboration_waiver_classes=(SourceClass.PRIMARY_ORG,),
        # The tightest window in the table. A 60-day-old careers page is already suspect.
        recency=RecencyRule(max_age_days=60, hard=True),
        min_corroboration=2,
        primary_source_required=True,
        risk_class=RiskClass.LOW,
        output_sections=("role_summary", "requirements", "employer_signals", "gaps"),
        query_shape_hints=("careers page", "job posting", "working at"),
    ),
    "consumer_purchase": SourcePolicy(
        policy_id="consumer_purchase",
        label="Consumer purchase",
        intent_summary=(
            "Which thing to buy for a stated use and budget, where somebody who does "
            "not sell it has to have tried it."
        ),
        preferred_source_classes=(SourceClass.INDEPENDENT_REVIEW, SourceClass.PRIMARY_ORG),
        acceptable_source_classes=(
            SourceClass.MARKETPLACE_LISTING, SourceClass.COMMUNITY_FORUM, SourceClass.TRADE_PRESS,
        ),
        discouraged_source_classes=(SourceClass.AGGREGATOR, SourceClass.VENDOR_MARKETING),
        required_source_class_groups=((SourceClass.INDEPENDENT_REVIEW,),),
        recency=RecencyRule(max_age_days=180, hard=False),
        min_corroboration=2,
        independence_required=True,
        risk_class=RiskClass.LOW,
        output_sections=("recommendation", "independent_testing", "price_and_availability", "gaps"),
        query_shape_hints=("independent review", "tested", "best for"),
    ),
    GENERIC_POLICY_ID: SourcePolicy(
        policy_id=GENERIC_POLICY_ID,
        label="Open question",
        intent_summary=(
            "Anything that does not clearly match another shape. The conservative "
            "default, not a permissive one."
        ),
        preferred_source_classes=(
            SourceClass.PRIMARY_ORG, SourceClass.ESTABLISHED_NEWS,
            SourceClass.REGULATOR_GOV, SourceClass.PEER_REVIEWED,
        ),
        acceptable_source_classes=(
            SourceClass.TRADE_PRESS, SourceClass.DOCS_REFERENCE,
            SourceClass.INDEPENDENT_REVIEW, SourceClass.STANDARDS_BODY,
        ),
        discouraged_source_classes=(SourceClass.AGGREGATOR, SourceClass.PERSONAL_BLOG),
        recency=RecencyRule(max_age_days=365, hard=False),
        # Deliberately STRICTER than several named rows: two-source corroboration,
        # medium risk, mandatory disclaimer. An unrecognised question gets MORE caution,
        # not less, which is the opposite of the usual fallback failure mode.
        min_corroboration=2,
        risk_class=RiskClass.MEDIUM,
        requires_disclaimer=True,
        disclaimer_keys=("general_information",),
        output_sections=("answer", "evidence", "uncertainty", "gaps"),
        query_shape_hints=("authoritative source", "official statement", "recent reporting"),
    ),
}

# What the classifier is shown: policy_id and intent_summary only. It never sees a
# domain name, a topic list, or the strictness of a row, so it cannot learn to pick the
# cheap row to get an easier job.
CLASSIFIER_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (row.policy_id, row.intent_summary) for row in POLICY_TABLE.values()
)
