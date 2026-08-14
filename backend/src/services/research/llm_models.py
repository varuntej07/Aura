"""Structured-output contracts. One model per model call, and nothing else.

These are the ONLY channel out of a model in this package. No stage persists free-form
model text, and no stage forwards it. That is not a style preference: the reading,
verification and synthesis stages all consume untrusted third-party page content, and a
schema is the boundary that stops a page's prose from becoming a field value.

Every model here is frozen with ``extra="forbid"`` and carries hard length and count
bounds. A model that invents an extra field, or writes a 40,000-character "excerpt",
fails validation and the stage retries or degrades. Validation failure is a cheaper
outcome than persisting a hallucination, which is why the bounds are tight rather than
generous.

The load-bearing one is ``ClaimCandidate.evidence_excerpt``: ``min_length=1``. A claim
with no supporting span cannot be instantiated, so an unsupported assertion has no
representation to travel through the pipeline in. That structural guarantee, not a
prompt asking for honesty, is what makes "every claim is backed by a URL and an
excerpt" true.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .models import EntityBinding, ScopeDimension
from .policy_table import SourceClass

# --- classification ----------------------------------------------------------------


class ClarificationQuestion(BaseModel):
    """One compact question, which may group up to three tightly related fields.

    Grouped on purpose. Asking geography, buyer segment and time range as three separate
    rounds is an interrogation; asking them as one question with three fields is a single
    decision for the user. At most two rounds ever happen, and the second is allowed only
    when the first answer creates a NEW material ambiguity.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=400)
    # What the run will assume if the user picks "use these assumptions" instead of
    # answering. Never empty: a question the user can only answer, never skip, is a
    # blocking prompt, and this workflow must always be able to proceed without one.
    default_assumptions: list[str] = Field(min_length=1, max_length=3)
    choices: list[str] = Field(default_factory=list, max_length=6)


class ClassifiedSubQuestion(BaseModel):
    """One decomposed question with explicit links to the entities it asks about."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    must_answer: bool = False
    entity_bindings: list[EntityBinding] = Field(default_factory=list, max_length=4)


class ClassificationResult(BaseModel):
    """The classifier's whole output. One call, no tools, no domain names, no topics.

    ``profile_ids`` names rows in the policy table by their epistemic SHAPE, which is why
    the prompt can carry the entire table without ever listing a topic. An unrecognised
    id is coerced to the conservative generic row by ``policy.resolve_profile_ids``
    rather than crashing or silently selecting row 0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_ids: list[str] = Field(min_length=1, max_length=3)
    profile_confidence: float = Field(ge=0.0, le=1.0)

    objective: str = Field(min_length=1, max_length=1000)
    entities: list[str] = Field(default_factory=list, max_length=12)
    criteria: list[str] = Field(default_factory=list, max_length=10)
    jurisdiction: str = Field(default="", max_length=120)
    language: str = Field(default="en", max_length=16)
    time_anchor: str = Field(default="", max_length=64)

    sub_questions: list[ClassifiedSubQuestion] = Field(default_factory=list, max_length=12)
    seed_queries: list[str] = Field(min_length=1, max_length=12)
    assumptions: list[str] = Field(default_factory=list, max_length=6)

    # Non-empty risk flags floor the composed risk class at medium and force a
    # disclaimer, regardless of which rows were named. A novel topic gets MORE caution.
    risk_flags: list[str] = Field(default_factory=list, max_length=6)

    # True only when a missing field would change what gets researched, not merely
    # sharpen it. A run that can proceed on stated assumptions should proceed.
    needs_clarification: bool = False
    question: ClarificationQuestion | None = None


# --- per-source extraction ----------------------------------------------------------


class ExtractedScopeQualifier(BaseModel):
    """A scope qualifier proposed from one claim's authoritative excerpt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: ScopeDimension
    value: str = Field(min_length=1, max_length=120)
    evidence_excerpt: str = Field(min_length=1, max_length=200)


class ClaimCandidate(BaseModel):
    """One assertion a single page supports, before any cross-source merge.

    Candidates live on their OWN source document and are never written into a shared
    claim doc by a child. Concurrent readers colliding on ``claims/{id}`` would race on
    the evidence array and create a hot document; the single-owner ``verify`` stage does
    the merge instead.

    ``subject``, ``attribute`` and ``value_normalized`` are normalized HERE, by the
    model, because that normalization is the only reason "starts at $20/mo" and "twenty
    dollars a month" later group together for contradiction detection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sub_question_id: str = Field(min_length=1, max_length=64)
    claim_kind: str = Field(min_length=1, max_length=24)

    subject: str = Field(min_length=1, max_length=200)
    attribute: str = Field(min_length=1, max_length=200)
    value_normalized: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=1000)

    # min_length=1 is the structural guarantee. No span, no claim.
    evidence_excerpt: str = Field(min_length=1, max_length=400)
    scope_qualifiers: list[ExtractedScopeQualifier] = Field(
        default_factory=list, max_length=6
    )


class PageExtraction(BaseModel):
    """What one page yielded. Produced by a TOOL-FREE model call.

    The reading stage passes no ``tools=`` argument at all, so there is no capability
    present for injected page text to hijack. This is a deliberate divergence from the
    chat reasoning loop, which does hand a model ``web_surf`` while feeding it web
    content; that pattern is exactly what must not be copied here.

    ``injection_suspected`` is the page reporting on itself. A document that tries to
    issue instructions is content to be REPORTED, never followed, and setting this flag
    forces ``source_class`` to UNTRUSTED in code: the claims are excluded from
    corroboration and cannot satisfy a must-answer, while the source still appears in
    the brief's transparency section because the user should know a page tried it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Empty when the page declared none. Absence is reported as "undated" rather than
    # guessed, because a guessed publication date silently defeats every freshness rule.
    published_at: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=1000)
    claims: list[ClaimCandidate] = Field(default_factory=list, max_length=12)
    injection_suspected: bool = False
    # Why the page yielded nothing useful, when it yielded nothing useful. Lets a
    # boilerplate or navigation-only page become a typed gap instead of silence.
    unusable_reason: str = Field(default="", max_length=120)


# --- domain classification ----------------------------------------------------------


class DomainClass(BaseModel):
    """One domain's epistemic role, learned rather than listed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: str = Field(min_length=1, max_length=253)
    source_class: SourceClass
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=200)


class DomainClassBatch(BaseModel):
    """Up to 25 domains classified in ONE call, then cached globally for 30 days.

    Batched and cached because a domain's role is not user data and amortising it across
    every user is the entire point. The input is domain, page title and search snippet,
    NEVER page body: classifying a domain from content we have not yet decided to trust
    would let the page influence its own trust rating.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    domains: list[DomainClass] = Field(default_factory=list, max_length=25)


# --- contradiction adjudication -----------------------------------------------------


class AdjudicationVerdict(StrEnum):
    """The only four relationships the adjudicator may report."""

    AGREEMENT = "agreement"
    # The values differ because they describe different things (plan tier, jurisdiction,
    # year). Both survive, each carrying the distinguishing scope.
    DIFFERENT_SCOPE = "different_scope"
    # One value replaced the other over time. The older stays visible, marked stale.
    SUPERSEDED_BY_RECENCY = "superseded_by_recency"
    # An unexplained disagreement. Both values stay visible and neither may answer.
    CONTRADICTION = "contradiction"


class CandidateScopeReference(BaseModel):
    """One reference to a qualifier validated before adjudication."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=32)
    qualifier_id: str = Field(min_length=1, max_length=32)


class Adjudication(BaseModel):
    """How two competing values for the same (subject, attribute) relate.

    The adjudicator has no tools and CANNOT write a new value. It may only classify a
    relationship between values that already carry evidence, which is what stops
    "resolving" a disagreement into a third invented number.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # A closed set, not a free string. The consumer compares against exactly these four
    # values, so anything else silently fell through to "not agreement, not superseded",
    # which happens to mean contradiction - reached by accident rather than by decision.
    # Making it an enum turns an unparseable verdict into a retry instead.
    verdict: AdjudicationVerdict
    # Required for different_scope. The adjudicator may select only code-assigned ids
    # for qualifiers that were grounded before this call. It cannot submit dimension,
    # value, or excerpt metadata of its own.
    scope_references: list[CandidateScopeReference] = Field(
        default_factory=list, max_length=12
    )
    # Required for superseded_by_recency: which value_normalized wins.
    winning_value: str = Field(default="", max_length=300)
    rationale: str = Field(default="", max_length=300)


# --- synthesis ----------------------------------------------------------------------


class FactualStatement(BaseModel):
    """One atomic statement, expressed as a SELECTION of stored claims.

    The model no longer writes the factual sentence. It chooses which stored claims to
    state and in what order, and ``synthesize`` renders the sentence in code from those
    claims' canonical text, which is itself derived from the excerpt verified against the
    page.

    This replaces a free-form paragraph carrying a citation list, and the reason is that
    the paragraph was never checkable. Citations were verified, prose was not, so one
    valid claim id licensed up to 350 characters of whatever the model wanted to say
    beside it - and the enforcement pass, having found a real id, kept all of it. Shrinking
    the paragraph made the licence smaller without making it legitimate. The only way one
    valid id can stop licensing unrelated prose is for there to be no unrelated prose.

    There is deliberately no prose field. One valid claim id must never license adjacent
    model-authored words that look cited but were not established by that claim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # At most three, because a statement resting on more than three claims is not atomic
    # and the attribution stops being answerable at a glance.
    claim_ids: list[str] = Field(min_length=1, max_length=3)


class BriefSection(BaseModel):
    """One section of the finished brief, as an ordered list of atomic statements.

    Citations are verified in code after parsing: a statement whose claim ids all fail to
    resolve is dropped, and a section left with no statements is deleted and converted
    into a gap. The model cannot write a fact into the brief without a persisted,
    excerpt-backed, URL-bearing claim document behind it, and now it cannot write the
    sentence either.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Index into the code-owned section list supplied in the prompt. The model selects a
    # section but never writes visible heading text.
    section_index: int = Field(ge=0, le=11)
    statements: list[FactualStatement] = Field(min_length=1, max_length=20)


class Brief(BaseModel):
    """The finished artifact. Bounded so it cannot approach Firestore's 1 MB limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The summary is the most-read and most-quoted part of the brief, spoken aloud by
    # voice and shown in a card, and it was the one part with no citation requirement at
    # all: every section had to be backed while the paragraph most likely to be repeated
    # out of context could say anything.
    #
    # It is now held to exactly the rule the body is held to, and for the same reason:
    # free prose plus a citation list is not attribution, it is a citation list next to
    # free prose. The summary is a short ordered selection of stored claims, rendered in
    # code from their canonical text.
    executive_summary: list[FactualStatement] = Field(
        default_factory=list, max_length=6
    )
    sections: list[BriefSection] = Field(default_factory=list, max_length=12)
