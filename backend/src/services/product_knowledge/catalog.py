"""Strict product catalog loading and deterministic local retrieval.

The model decides whether to call the product-information tool. This module only
ranks entries after that structural decision, so it never acts as an intent gate.

``kind`` is a model guess, so it is a ranking prior and never a filter. It used to
select the candidate pool, which meant one miscategorization could hide the correct
entry entirely: ``kind="capabilities"`` returned the surface brochure without ever
reading ``query``, so "can you change your voice" was answered with the full feature
list instead of how to change it. Retrieval now ranks the whole eligible catalog and
the prior only breaks ties, so a strong match outvotes a wrong category.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ProductInfoKind = Literal[
    "capabilities",
    "how_to",
    "availability",
    "privacy",
    "product_background",
    "troubleshooting",
]
ProductSurface = Literal["app", "keyboard", "desktop", "all"]
CurrentProductSurface = Literal["app", "keyboard", "desktop"]
ProductTargetSurface = Literal["current", "app", "keyboard", "desktop", "all"]
ProductTargetPlatform = Literal["current", "android", "ios", "windows", "macos", "all"]
ResponseChannel = Literal["voice", "chat"]

_CONTENT_PATH = Path(__file__).parent / "content" / "product_knowledge_v1.json"
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Retrieval-only stop words. They run after the model has selected the product tool;
# they never decide user intent or authorize an action.
_RETRIEVAL_STOP_WORDS = frozenset(
    {
        "a", "an", "are", "can", "could", "did", "do", "does", "for", "here",
        "how", "i", "in", "is", "it", "me", "my", "of", "on", "please", "the",
        "to", "what", "where", "who", "would", "you", "your",
    }
)
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")
_MAX_QUERY_CHARS = 500
_MAX_RESULTS = 3
_MIN_RETRIEVAL_SCORE = 3.0
# The model's ``kind`` guess is evidence, not a fact, so it reorders results but is
# deliberately kept out of the fail-closed floor: a wrong-kind entry must never become
# answerable on the bonus alone. Measured against the live catalog, correct answers
# survive anywhere in 0.0-2.0 and start breaking at 3.0, where the prior inverts a
# genuine 2-point retrieval gap ("how do I upgrade" under kind="troubleshooting").
_KIND_PRIOR_BONUS = 1.0
_SURFACE_QUERY_NOISE: dict[ProductSurface, frozenset[str]] = {
    "app": frozenset({"app", "mobile", "phone"}),
    "keyboard": frozenset({"keyboard"}),
    "desktop": frozenset({"desktop", "laptop", "pc"}),
    "all": frozenset(),
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductAnswers(_StrictModel):
    # Voice answers are spoken verbatim, so length is latency the user sits through.
    # The 800 cap let three capability entries grow into ~600-character brochures that
    # took roughly 45 seconds to read out, including as the reply to a follow-up
    # "what else". 320 is comfortably above the longest real answer and far below a
    # brochure, and catalog validation runs at import so a regression fails startup.
    voice: str = Field(min_length=1, max_length=320)
    chat: str = Field(min_length=1, max_length=2400)


class ProductAvailability(_StrictModel):
    tiers: tuple[str, ...] = Field(min_length=1)
    connectors: tuple[str, ...]
    permissions: tuple[str, ...]
    minimum_versions: dict[str, str]

    @model_validator(mode="after")
    def validate_versions(self) -> ProductAvailability:
        invalid = [
            value
            for value in self.minimum_versions.values()
            if not _VERSION_RE.fullmatch(value)
        ]
        if invalid:
            raise ValueError(f"Invalid minimum version: {invalid[0]}")
        return self


class ProductEvidence(_StrictModel):
    path: str = Field(min_length=1, max_length=300)
    symbol: str = Field(min_length=1, max_length=200)
    verified_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class ProductClientRelease(_StrictModel):
    surface: Literal["app", "keyboard", "desktop"]
    platform: Literal["android", "ios", "windows", "macos"]
    availability: Literal["available", "unavailable"]
    distribution: str = Field(min_length=1, max_length=160)


class ProductRelease(_StrictModel):
    stage: Literal["beta", "general_availability"]
    answers: ProductAnswers
    clients: tuple[ProductClientRelease, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_clients(self) -> ProductRelease:
        keys = [(client.surface, client.platform) for client in self.clients]
        if len(keys) != len(set(keys)):
            raise ValueError("Product release client rows must be unique")
        if not any(client.availability == "available" for client in self.clients):
            raise ValueError("Product release must include an available client")
        return self


class ProductEntry(_StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,79}$")
    kind: ProductInfoKind
    status: Literal["shipped", "beta", "deprecated"]
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=500)
    answers: ProductAnswers
    surfaces: tuple[ProductSurface, ...] = Field(min_length=1)
    platforms: tuple[Literal["android", "ios", "windows", "macos", "all"], ...] = Field(min_length=1)
    availability: ProductAvailability
    navigation: tuple[str, ...]
    action_tools: tuple[str, ...]
    search_terms: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[ProductEvidence, ...] = Field(min_length=1)


class ProductCatalog(_StrictModel):
    schema_version: Literal[1]
    knowledge_version: str = Field(pattern=r"^\d{4}\.\d{2}\.\d{2}(?:\.\d+)?$")
    product_release: ProductRelease
    entries: tuple[ProductEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> ProductCatalog:
        ids = [entry.id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("Product knowledge entry IDs must be unique")

        release_entry = next(
            (entry for entry in self.entries if entry.id == "availability.product_release"),
            None,
        )
        if release_entry is None or release_entry.answers != self.product_release.answers:
            raise ValueError(
                "availability.product_release must mirror product_release answers"
            )

        unavailable = {
            (client.surface, client.platform)
            for client in self.product_release.clients
            if client.availability == "unavailable"
        }
        for entry in self.entries:
            if entry.kind == "availability":
                continue
            for surface in entry.surfaces:
                for platform in entry.platforms:
                    if (surface, platform) in unavailable:
                        raise ValueError(
                            f"{entry.id} claims unavailable client {surface}/{platform}"
                        )
        return self


class ProductKnowledgeResult(_StrictModel):
    answer: str
    matched: bool
    entry_ids: tuple[str, ...]
    knowledge_version: str
    confidence: float = Field(ge=0.0, le=1.0)


def _load_catalog() -> ProductCatalog:
    raw = json.loads(_CONTENT_PATH.read_text(encoding="utf-8"))
    return ProductCatalog.model_validate(raw)


# Load and validate at process import so malformed production content fails loudly
# during startup rather than returning invented or partially parsed answers.
PRODUCT_KNOWLEDGE = _load_catalog()


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_WORD_RE.findall(normalized))


def _tokens(value: str) -> list[str]:
    return [token for token in _normalize(value).split() if token not in _RETRIEVAL_STOP_WORDS]


def _character_ngrams(value: str, size: int = 3) -> set[str]:
    compact = _normalize(value).replace(" ", "_")
    if len(compact) <= size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _version_tuple(value: str) -> tuple[int, ...] | None:
    candidate = value.strip()
    if not _VERSION_RE.fullmatch(candidate):
        return None
    return tuple(int(part) for part in candidate.split("."))


def _version_is_eligible(entry: ProductEntry, platform: str, app_version: str) -> bool:
    required = entry.availability.minimum_versions.get(platform)
    if not required:
        return True
    actual_parts = _version_tuple(app_version)
    required_parts = _version_tuple(required)
    if actual_parts is None or required_parts is None:
        return False
    width = max(len(actual_parts), len(required_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= required_parts + (0,) * (
        width - len(required_parts)
    )


def _surface_is_eligible(entry: ProductEntry, surface: ProductSurface) -> bool:
    return "all" in entry.surfaces or surface == "all" or surface in entry.surfaces


def _platform_is_eligible(entry: ProductEntry, platform: str) -> bool:
    return not platform or "all" in entry.platforms or platform in entry.platforms


def _document_tokens(entry: ProductEntry) -> list[str]:
    # Field repetition is an explicit, inspectable weight: titles and editor-written
    # aliases matter more than long answer prose, which prevents generic copy from
    # outranking the feature the user actually named.
    fields = (
        [entry.title] * 5
        + list(entry.search_terms) * 4
        + [entry.summary] * 2
        + list(entry.navigation)
        + list(entry.action_tools)
    )
    return _tokens(" ".join(fields))


def _rank(
    query: str,
    entries: list[ProductEntry],
    *,
    ignored_query_tokens: frozenset[str] = frozenset(),
    preferred_kind: ProductInfoKind | None = None,
) -> list[tuple[ProductEntry, float, float, float]]:
    """Rank entries as ``(entry, base_score, ordered_score, named_field_coverage)``,
    best ordering first.

    ``base_score`` is retrieval evidence alone and is what callers must test against
    ``_MIN_RETRIEVAL_SCORE``. ``ordered_score`` adds the ``preferred_kind`` prior and
    is only for ordering. ``named_field_coverage`` is how much of the query an
    editor-authored name actually covers; it feeds confidence so a hit carried by
    generic tokens cannot report certainty.
    """
    if not entries:
        return []
    query_tokens = [
        token for token in _tokens(query) if token not in ignored_query_tokens
    ]
    if not query_tokens:
        return []
    documents = [_document_tokens(entry) for entry in entries]
    average_length = sum(len(document) for document in documents) / len(documents)
    query_counts = Counter(query_tokens)
    document_frequencies = Counter(
        token for token in set(query_tokens) for document in documents if token in set(document)
    )
    query_ngrams = _character_ngrams(query)
    ranked: list[tuple[ProductEntry, float, float, float]] = []
    for entry, document in zip(entries, documents, strict=True):
        frequencies = Counter(document)
        bm25 = 0.0
        for token, query_frequency in query_counts.items():
            frequency = frequencies[token]
            if not frequency:
                continue
            document_frequency = document_frequencies[token]
            inverse_document_frequency = math.log(
                1.0 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + 1.2 * (
                1.0 - 0.75 + 0.75 * len(document) / max(average_length, 1.0)
            )
            bm25 += query_frequency * inverse_document_frequency * (frequency * 2.2 / denominator)
        document_ngrams = _character_ngrams(" ".join(document))
        union = query_ngrams | document_ngrams
        character_similarity = len(query_ngrams & document_ngrams) / len(union) if union else 0.0
        coverage = len(set(query_tokens) & set(document)) / len(set(query_tokens))
        query_token_set = set(query_tokens)
        named_fields = [entry.title, *entry.search_terms]
        named_field_token_sets = [set(_tokens(field)) for field in named_fields]
        named_field_coverage = max(
            (
                len(query_token_set & field_tokens) / len(query_token_set)
                for field_tokens in named_field_token_sets
            ),
            default=0.0,
        )
        named_field_precision = max(
            (
                len(query_token_set & field_tokens) / len(field_tokens)
                for field_tokens in named_field_token_sets
                if field_tokens
            ),
            default=0.0,
        )
        # Editor-authored names and aliases are stronger evidence than a generic
        # navigation word. This lets a one-word query such as "settings" clear the
        # fail-closed floor only for an entry that explicitly names Settings.
        score = (
            bm25
            + (2.0 * coverage)
            + (3.5 * named_field_coverage)
            + (3.0 * named_field_precision)
            + (1.5 * character_similarity)
        )
        ordered_score = score + (
            _KIND_PRIOR_BONUS if preferred_kind and entry.kind == preferred_kind else 0.0
        )
        ranked.append((entry, score, ordered_score, named_field_coverage))
    return sorted(ranked, key=lambda item: (-item[2], item[0].id))


def _capability_entry(entries: list[ProductEntry], surface: ProductSurface) -> ProductEntry | None:
    preferred_id = f"capabilities.{surface}"
    return next((entry for entry in entries if entry.id == preferred_id), None) or next(
        (entry for entry in entries if entry.id == "capabilities.all"), None
    )


def lookup_product_knowledge(
    *,
    kind: ProductInfoKind,
    query: str,
    target_surface: ProductTargetSurface,
    target_platform: ProductTargetPlatform = "current",
    current_surface: CurrentProductSurface = "app",
    platform: str = "",
    app_version: str = "",
    channel: ResponseChannel = "chat",
) -> ProductKnowledgeResult:
    """Return a verified answer or a fail-closed no-match response."""
    surface: ProductSurface = current_surface if target_surface == "current" else target_surface
    bounded_query = query.strip()[:_MAX_QUERY_CHARS]
    normalized_platform = platform.strip().casefold()
    filter_platform = (
        normalized_platform
        if target_platform == "current"
        else "" if target_platform == "all" else target_platform
    )

    release_clients = [
        client
        for client in PRODUCT_KNOWLEDGE.product_release.clients
        if client.platform == filter_platform
        and (surface == "all" or client.surface == surface)
    ]
    if release_clients and all(
        client.availability == "unavailable" for client in release_clients
    ):
        return ProductKnowledgeResult(
            answer=getattr(PRODUCT_KNOWLEDGE.product_release.answers, channel),
            matched=True,
            entry_ids=("availability.product_release",),
            knowledge_version=PRODUCT_KNOWLEDGE.knowledge_version,
            confidence=1.0,
        )
    # Surface, platform and version stay hard filters: those are structural facts the
    # caller supplies about the client, not model judgement. kind is model judgement,
    # so it does not narrow the pool.
    eligible = [
        entry
        for entry in PRODUCT_KNOWLEDGE.entries
        if entry.status != "deprecated"
        and _surface_is_eligible(entry, surface)
        and _platform_is_eligible(entry, filter_platform)
        and _version_is_eligible(entry, filter_platform, app_version)
    ]

    # Surface is already a typed retrieval filter. Removing its generic nouns from
    # non-availability ranking prevents "app settings" from favoring every entry
    # whose navigation happens to mention the app. Platform words deliberately stay
    # searchable for questions such as whether an iPhone release exists.
    ignored_query_tokens = frozenset()
    if kind != "availability":
        # The model has already supplied surface and platform as typed fields, so
        # their generic nouns should not compete with the actual feature name.
        ignored_query_tokens = _SURFACE_QUERY_NOISE[surface] | frozenset(
            {filter_platform} if filter_platform else ()
        )
    ranked = _rank(
        bounded_query,
        eligible,
        ignored_query_tokens=ignored_query_tokens,
        preferred_kind=kind,
    )
    # Fail closed on retrieval evidence alone. The kind prior reorders what survives
    # this cut; it can never lift an entry over the floor.
    answerable = [item for item in ranked if item[1] >= _MIN_RETRIEVAL_SCORE]

    if not answerable:
        if kind == "capabilities":
            # A broad question naming no feature. The overview is the answer, which
            # keeps the pre-existing guarantee that Buddy always has one.
            overview = _capability_entry(
                [entry for entry in eligible if entry.kind == kind], surface
            )
            if overview is not None:
                return ProductKnowledgeResult(
                    answer=getattr(overview.answers, channel),
                    matched=True,
                    entry_ids=(overview.id,),
                    knowledge_version=PRODUCT_KNOWLEDGE.knowledge_version,
                    confidence=1.0,
                )
        # This string can reach the user's ears verbatim, so it must be speakable
        # product voice, never internal vocabulary ("guide", "catalog"): a live
        # 2026-09 session read "the product guide doesn't cover that" aloud.
        fallback = (
            "I'm not sure about that part of Aura yet."
            if channel == "voice"
            else "I’m not sure about that part of Aura yet."
        )
        return ProductKnowledgeResult(
            answer=fallback,
            matched=False,
            entry_ids=(),
            knowledge_version=PRODUCT_KNOWLEDGE.knowledge_version,
            confidence=0.0,
        )

    # Voice speaks the answer verbatim and every entry answer is authored to stand
    # alone, so stacking near-ties there only lengthens the spoken reply. Chat can
    # afford the extra context.
    result_limit = 1 if channel == "voice" else _MAX_RESULTS
    top_ordered = answerable[0][2]
    selected = [
        item for item in answerable[:result_limit] if item[2] >= top_ordered * 0.90
    ]
    answers = [getattr(entry.answers, channel).strip() for entry, _, _, _ in selected]
    separator = " " if channel == "voice" else "\n\n"
    # Confidence must reflect whether an editor-authored name matched, not just
    # accumulated generic-token score: "capture entire stream recording setup"
    # used to report 1.0 for the screen-privacy entry. Retrieval score still
    # scales the value, but without named-field evidence it can never claim
    # certainty. This is advisory for the model's phrasing; the 3.0 floor above
    # remains the only hard gate.
    confidence = min(1.0, answerable[0][1] / 6.0) * (
        0.25 + 0.75 * answerable[0][3]
    )
    return ProductKnowledgeResult(
        answer=separator.join(dict.fromkeys(answers)),
        matched=True,
        entry_ids=tuple(entry.id for entry, _, _, _ in selected),
        knowledge_version=PRODUCT_KNOWLEDGE.knowledge_version,
        confidence=confidence,
    )
