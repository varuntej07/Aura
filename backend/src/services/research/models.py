"""Contracts for research page acquisition and for one research run.

Frozen pydantic models with `extra="forbid"`, matching services/dictation/models.py.
Every failure is a value from a closed StrEnum, never a provider exception string, so
a later persistence phase can store the reason and a UI can render it without ever
surfacing a stack trace or a vendor error message to the user.

Two families live here. The first is page acquisition, which the Firecrawl adapter and
`acquire.py` already use. The second, from `ResearchRequest` down, is the run itself:
the user's immutable request, the versioned interpretation of it, and the evidence
model. Nothing writes the second family yet.

The evidence models are where the product's central promise is enforced in TYPES rather
than in prompts. `Evidence` requires a URL, a verbatim excerpt and a retrieval time;
`Claim` requires at least one `Evidence`. A claim with no source is therefore not
something the pipeline drops later, it is something that cannot be constructed at all.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .budget import Preset
from .policy_table import SourceClass, SourcePolicy


class UrlRejectReason(StrEnum):
    """Why a URL was refused before any provider ever saw it.

    REDIRECT_BLOCKED is the only member produced after a read: it means the page the
    provider actually landed on, after redirects, fails the same policy the requested
    URL passed.
    """

    MALFORMED = "malformed"
    TOO_LONG = "too_long"
    NOT_HTTPS = "not_https"
    CREDENTIALS_IN_URL = "credentials_in_url"
    SIGNED_URL = "signed_url"
    NO_HOST = "no_host"
    SINGLE_LABEL_HOST = "single_label_host"
    RESERVED_SUFFIX = "reserved_suffix"
    METADATA_HOST = "metadata_host"
    PRIVATE_ADDRESS = "private_address"
    DNS_FAILED = "dns_failed"
    REDIRECT_BLOCKED = "redirect_blocked"


class PageReadState(StrEnum):
    """The stable outcome of one page read.

    Only OK carries usable markdown. Everything else is a gap a research run records
    and continues past; none of them is an error the caller raises on.
    """

    OK = "ok"
    BLOCKED = "blocked"
    PAYWALLED = "paywalled"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    URL_NOT_ALLOWED = "url_not_allowed"


class UrlVerdict(BaseModel):
    """The result of the public-URL policy for one candidate URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    # Canonicalized even when rejected, so dedupe and logging use one stable key.
    canonical_url: str = Field(max_length=2048)
    reason: UrlRejectReason | None = None


class PageReadRequest(BaseModel):
    """One bounded read of one already-validated public URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    timeout_s: float = Field(gt=0, le=120)
    max_chars: int = Field(gt=0, le=2_000_000)
    # The RAW RESPONSE byte ceiling, enforced on the transport: the reader stops reading
    # the body at this many bytes and reports TOO_LARGE.
    #
    # Distinct from max_chars, which is a ceiling on the text KEPT after the whole
    # response has already been received and parsed. Clipping characters off a document
    # that has already crossed the wire bounds what is stored, not what is downloaded, and
    # the two were being conflated: a 40 MB response was fully received, fully parsed and
    # then trimmed to 120k characters, with the byte meter recording the 120k.
    max_bytes: int = Field(default=8_000_000, gt=0, le=100_000_000)
    # Firecrawl page credits this read may bill. A basic scrape is one credit; a PDF bills
    # one credit PER PDF PAGE, which cannot be bounded after the fact. When a request
    # cannot be shown to fit this, the reader refuses to send it rather than discovering
    # the overrun on the invoice.
    max_page_credits: int = Field(default=8, gt=0, le=1_000)
    # Correlates the provider call with the run/stage that asked for it. Never a uid.
    correlation_id: str = Field(default="", max_length=64)
    feature: str = Field(default="research_acquire", max_length=64)


class PageReadResult(BaseModel):
    """What a PageReader returns for one URL, success or not.

    `markdown` is untrusted third-party content and lives in process memory only. A
    later persistence phase stores `content_sha256` and `char_count` instead, which is
    enough to prove what was read and to detect a page that changed between waves.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_url: str = Field(max_length=2048)
    canonical_url: str = Field(max_length=2048)
    # Where the provider actually landed after redirects. Falls back to canonical_url
    # when the provider reports nothing.
    final_url: str = Field(default="", max_length=2048)
    state: PageReadState

    title: str = Field(default="", max_length=512)
    # ISO 8601 string, not a datetime, so this stays trivially JSON-serializable.
    published_at: str = Field(default="", max_length=64)
    content_type: str = Field(default="", max_length=128)
    language: str = Field(default="", max_length=32)

    markdown: str = ""
    char_count: int = Field(default=0, ge=0)
    # UTF-8 bytes actually RECEIVED from the provider, counted on the transport before
    # anything was parsed or clipped. This is what the byte ledger meters; char_count is
    # what survived into memory, and the two are only equal by coincidence.
    received_bytes: int = Field(default=0, ge=0)
    # False only when a local preflight refused the URL before the Firecrawl request.
    request_sent: bool = False
    truncated: bool = False
    content_sha256: str = Field(default="", max_length=64)

    # Provider credits this read consumed, when the provider reports it. A basic
    # Firecrawl scrape is 1 credit; a PDF costs 1 per page, so this is not always 1.
    credits_used: int | None = None
    # The status the TARGET SITE returned, as reported by the provider. Distinct from
    # the provider's own HTTP status, which is about our account, not the page.
    status_code: int | None = None
    failure_reason: str = Field(default="", max_length=64)


class RejectedUrl(BaseModel):
    """A candidate that never became a read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(max_length=2048)
    reason: UrlRejectReason


class AcquisitionResult(BaseModel):
    """Everything one Brave-plus-reader acquisition produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    # Brave's raw [{title, url}] list, before the URL policy ran.
    search_sources: list[dict[str, str]] = Field(default_factory=list)
    pages: list[PageReadResult] = Field(default_factory=list)
    rejected: list[RejectedUrl] = Field(default_factory=list)
    # True when Brave served this query from its in-process cache (no billable search).
    brave_cached: bool = False


# --- the run: immutable request, versioned interpretation --------------------------
# The user's intent and the model's reading of it are separate records on purpose. A
# stage never rewrites what the user asked for, and a clarification answer creates a new
# plan version rather than mutating history, so "what did we actually agree to run" is
# always answerable after the fact.


class ResearchRequest(BaseModel):
    """What the user asked for, verbatim. Never edited by any stage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    # The idempotency key. A dashboard create uses a UUIDv4, chat uses the stable client
    # message id, voice derives it from the session plus the normalized request, so a
    # replayed delivery returns the same run instead of starting a second one.
    client_run_id: str = Field(min_length=1, max_length=128)
    # Preserved exactly as typed or transcribed. Derived fields (entities, jurisdiction,
    # time anchor) belong to the plan and never overwrite this.
    request: str = Field(min_length=1, max_length=2000)
    preset: Preset = Preset.QUICK
    origin_surface: Literal["chat", "voice", "dashboard"]
    submitted_at: datetime


class EntityBindingStatus(StrEnum):
    """Whether a sub-question names one unambiguous entity from its admitted plan."""

    VALID = "valid"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    MISMATCHED = "mismatched"


class EntityBinding(BaseModel):
    """One explicit link from a sub-question to an entity in the admitted plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str = Field(min_length=1, max_length=120)


class SubQuestion(BaseModel):
    """One answerable piece of the objective.

    ``must_answer`` is the load-bearing flag. Under `partial_over_guess`, which is true
    on every policy row, a must-answer with no corroborated claim is force-added to the
    brief's gaps in code after the model has written its sections, regardless of what
    the model claimed. An optional sub-question simply goes unanswered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sub_question_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=500)
    must_answer: bool = False
    entity_bindings: list[EntityBinding] = Field(default_factory=list, max_length=4)
    entity_binding_status: EntityBindingStatus


class ResearchPlan(BaseModel):
    """One generated interpretation of the request. Versioned, never mutated.

    Admission pins ``plan_version`` onto the run, and every later stage refuses a
    different one. That is what stops a task delayed behind a clarification round from
    executing against a newer interpretation than the user confirmed and paid for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_version: int = Field(ge=1)
    # Which revision of the user's request this interprets. Bumped by a clarification
    # answer, so plan and answer can never be mismatched.
    request_revision: int = Field(ge=0)

    profile_ids: list[str] = Field(default_factory=list, max_length=3)
    profile_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    objective: str = Field(min_length=1, max_length=1000)
    entities: list[str] = Field(default_factory=list, max_length=12)
    criteria: list[str] = Field(default_factory=list, max_length=10)
    jurisdiction: str = Field(default="", max_length=120)
    language: str = Field(default="en", max_length=16)
    time_anchor: str = Field(default="", max_length=64)

    sub_questions: list[SubQuestion] = Field(default_factory=list, max_length=12)
    seed_queries: list[str] = Field(default_factory=list, max_length=12)
    clarification_ids: list[str] = Field(default_factory=list, max_length=2)
    assumptions: list[str] = Field(default_factory=list, max_length=6)

    # The composed policy, persisted rather than recomputed. This is the audit trail
    # answering "why did this brief demand three sources" without re-running anything.
    effective_policy: SourcePolicy


# --- evidence ----------------------------------------------------------------------


class Evidence(BaseModel):
    """One verbatim span from one page that supports one claim.

    ``retrieved_at`` is required and has no default: when a fact was READ is what makes
    a dated answer honest, and a default would let an undated read masquerade as a
    fresh one. ``published_at`` stays optional because plenty of pages declare nothing,
    and that absence is itself reported (freshness becomes "undated").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    # Clamped hard, and rendered as PLAIN TEXT rather than markdown. This span comes
    # from an untrusted page, so link targets inside it are discarded: the only URLs a
    # brief renders are ones our own fetch path produced.
    excerpt: str = Field(min_length=1, max_length=400)
    retrieved_at: datetime
    published_at: datetime | None = None
    source_class: SourceClass


class ScopeDimension(StrEnum):
    """Closed dimensions that may explain why two grounded values legitimately differ."""

    PLAN_TIER = "plan_tier"
    JURISDICTION = "jurisdiction"
    REGION = "region"
    TIME_PERIOD = "time_period"
    UNIT = "unit"
    POPULATION = "population"
    PRODUCT_VARIANT = "product_variant"


class ScopeQualifier(BaseModel):
    """A code-verified scope value copied from a claim's authoritative excerpt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: ScopeDimension
    value: str = Field(min_length=1, max_length=120)


class Claim(BaseModel):
    """One normalized assertion with its supporting evidence.

    ``subject``, ``attribute`` and ``value_normalized`` are normalized at extraction
    time by the model, which is the only reason "starts at $20/mo", "USD 20 monthly" and
    "twenty dollars a month" group together for contradiction detection. String
    manipulation would not do it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1, max_length=64)
    sub_question_id: str = Field(min_length=1, max_length=64)
    claim_kind: Literal["fact", "number", "date", "quote", "evaluative"]

    subject: str = Field(min_length=1, max_length=200)
    attribute: str = Field(min_length=1, max_length=200)
    # The contradiction key: two claims sharing (subject, attribute) but differing here
    # are what the adjudicator is asked to explain.
    value_normalized: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=1000)

    # min_length=1 is the whole point. An unsupported claim is not dropped downstream,
    # it cannot be instantiated, so a hallucinated fact has no representation to travel
    # through the pipeline in.
    evidence: list[Evidence] = Field(min_length=1, max_length=6)
    # Distinct DOMAINS among non-aggregator, non-untrusted evidence. Three pages on one
    # site corroborate nothing.
    support_domains: int = Field(default=1, ge=0)

    confidence: Literal["corroborated", "single_source", "disputed", "unverified"]
    freshness: Literal["current", "dated", "undated", "stale"]
    as_of: datetime | None = None

    # Mutual links written by the adjudicator. A disputed claim stays VISIBLE in the
    # brief with both values and both dates; nothing is silently resolved.
    contradicts: list[str] = Field(default_factory=list, max_length=6)
    superseded_by: str = Field(default="", max_length=64)
    scope: ScopeQualifier | None = None
    # Which policy rule admitted it, so the brief can explain itself.
    admitted_by: str = Field(default="", max_length=64)


class Gap(BaseModel):
    """Something the run could not establish. A first-class output, not an error.

    A brief with named gaps is the product; a brief that quietly infers past them is
    the failure mode this type exists to make visible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sub_question_id: str = Field(default="", max_length=64)
    # One of the FAIL_* codes in fields.py, never a provider exception string.
    reason: str = Field(min_length=1, max_length=64)
    detail: str = Field(default="", max_length=300)
