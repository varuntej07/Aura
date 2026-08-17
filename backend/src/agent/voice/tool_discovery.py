"""Provider-neutral discovery over Aura's canonical voice tool catalog."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from ...shared.tools import CORE_TOOLS, tool_definition
from .capabilities import (
    VOICE_TOOL_REGISTRY,
    Capability,
    ToolEffect,
    ToolRollout,
    VoiceSurface,
    VoiceToolCapability,
    tool_name,
)

TOOL_DISCOVERY_VERSION = "2026-08-13.1"
DEFAULT_MAX_RESULTS = 7
INTENT_EXPIRY_TURNS = 6
INTENT_EXPIRY_SECONDS = 300.0

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_SEARCH_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "available",
        "be",
        "by",
        "call",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "i",
        "if",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "them",
        "this",
        "to",
        "tool",
        "use",
        "user",
        "want",
        "when",
        "what",
        "which",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


class IntentAuthorizationState(StrEnum):
    NONE = "none"
    PENDING = "pending"
    GRANTED = "granted"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class IntentArgument:
    value: Any
    provenance: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class SuccessfulReceipt:
    tool_name: str
    received_at: float


@dataclass(slots=True)
class ActiveIntentState:
    active_capability: Capability | None = None
    active_tool: str | None = None
    active_objective: str = ""
    referenced_object: str = ""
    collected_arguments: dict[str, IntentArgument] = field(default_factory=dict)
    missing_arguments: frozenset[str] = frozenset()
    last_clarification: str = ""
    authorization_state: IntentAuthorizationState = IntentAuthorizationState.NONE
    successful_receipt: SuccessfulReceipt | None = None
    cancellation_requested: bool = False
    cancelled_at: float | None = None
    expires_at: float | None = None
    expires_after_turn: int | None = None

    def is_active(self, turn_index: int, now: float | None = None) -> bool:
        self.expire_if_needed(turn_index, now)
        return self.active_capability is not None and self.cancelled_at is None

    def expire_if_needed(self, turn_index: int, now: float | None = None) -> bool:
        now_value = now if now is not None else time.monotonic()
        expired = (
            (self.expires_at is not None and now_value >= self.expires_at)
            or (
                self.expires_after_turn is not None
                and turn_index > self.expires_after_turn
            )
        )
        if expired:
            self.clear()
        return expired

    def clear(self) -> None:
        self.active_capability = None
        self.active_tool = None
        self.active_objective = ""
        self.referenced_object = ""
        self.collected_arguments.clear()
        self.missing_arguments = frozenset()
        self.last_clarification = ""
        self.authorization_state = IntentAuthorizationState.NONE
        self.successful_receipt = None
        self.cancellation_requested = False
        self.cancelled_at = None
        self.expires_at = None
        self.expires_after_turn = None

    def begin(
        self,
        *,
        capability: Capability,
        tool_name_value: str,
        required_arguments: frozenset[str],
        active_objective: str,
        referenced_object: str,
        turn_index: int,
        effect: ToolEffect,
    ) -> None:
        if capability != self.active_capability:
            self.collected_arguments.clear()
            self.successful_receipt = None
            self.last_clarification = ""
        self.active_capability = capability
        self.active_tool = tool_name_value
        if active_objective:
            self.active_objective = active_objective[:1000]
        if referenced_object:
            self.referenced_object = referenced_object[:500]
        self.missing_arguments = frozenset(
            name
            for name in required_arguments
            if name not in self.collected_arguments
            or self.collected_arguments[name].value in (None, "")
        )
        self.authorization_state = (
            IntentAuthorizationState.GRANTED
            if effect is ToolEffect.READ
            else IntentAuthorizationState.PENDING
        )
        self.cancellation_requested = False
        self.cancelled_at = None
        self._extend(turn_index)

    # CANCELLED is currently unreachable: its only trigger was a hand-written list of
    # cancel words, and nothing in the voice path infers cancellation any more. The
    # mechanism is kept whole so a semantic canceller can re-attach to it without
    # rebuilding the state transitions or the eligibility check that reads them.
    def request_cancellation(self, turn_index: int) -> None:
        if self.active_capability is None:
            return
        self.cancellation_requested = True
        self.authorization_state = IntentAuthorizationState.CANCELLED
        self._extend(turn_index)

    def record_tool_call(
        self,
        metadata: VoiceToolCapability,
        arguments: Mapping[str, Any],
        *,
        provenance: str,
        turn_index: int,
    ) -> None:
        now = time.monotonic()
        self.active_capability = metadata.capability
        self.active_tool = metadata.name
        for name, value in arguments.items():
            if value is not None:
                self.collected_arguments[str(name)] = IntentArgument(
                    value=value,
                    provenance=provenance,
                    updated_at=now,
                )
        self.missing_arguments = frozenset(
            name
            for name in metadata.required_fields - metadata.empty_allowed_fields
            if name not in self.collected_arguments
            or self.collected_arguments[name].value in (None, "")
        )
        self.referenced_object = self._referenced_object_from(arguments)
        self.authorization_state = IntentAuthorizationState.GRANTED
        self.cancellation_requested = False
        self.cancelled_at = None
        self._extend(turn_index)

    def record_clarification(self, text: str, turn_index: int) -> None:
        if self.active_capability is None or not self.missing_arguments:
            return
        self.last_clarification = text.strip()[:500]
        self._extend(turn_index)

    def record_receipt(self, tool_name_value: str, turn_index: int) -> None:
        self.successful_receipt = SuccessfulReceipt(
            tool_name=tool_name_value,
            received_at=time.monotonic(),
        )
        self.missing_arguments = frozenset()
        self.authorization_state = IntentAuthorizationState.GRANTED
        self._extend(turn_index)

    def mark_cancelled(self, turn_index: int) -> None:
        if self.active_capability is None:
            return
        self.cancellation_requested = False
        self.cancelled_at = time.monotonic()
        self.authorization_state = IntentAuthorizationState.CANCELLED
        self.expires_after_turn = turn_index + 1

    def render_for_model(self, exposed_names: Sequence[str]) -> str:
        if self.active_capability is None:
            return ""
        exposed = set(exposed_names)
        if (
            self.active_tool
            and self.active_tool not in exposed
            and not self.cancellation_requested
        ):
            return ""
        collected = {
            name: {
                "value": item.value,
                "provenance": item.provenance,
            }
            for name, item in self.collected_arguments.items()
        }
        payload = {
            "active_capability": self.active_capability.value,
            "active_objective": self.active_objective or None,
            "referenced_object": self.referenced_object or None,
            "collected_arguments": collected,
            "missing_arguments": sorted(self.missing_arguments),
            "last_clarification": self.last_clarification or None,
            "authorization_state": self.authorization_state.value,
            "successful_receipt": (
                self.successful_receipt.tool_name if self.successful_receipt else None
            ),
            "cancellation_requested": self.cancellation_requested,
        }
        return (
            "<active_intent_state>"
            + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            + "</active_intent_state>"
        )

    def _extend(self, turn_index: int) -> None:
        self.expires_at = time.monotonic() + INTENT_EXPIRY_SECONDS
        self.expires_after_turn = turn_index + INTENT_EXPIRY_TURNS

    @staticmethod
    def _referenced_object_from(arguments: Mapping[str, Any]) -> str:
        for name, value in arguments.items():
            if name.endswith("_id") and value not in (None, ""):
                return str(value)[:500]
        for name, value in arguments.items():
            if name in {"title", "name", "message", "key", "request"}:
                if value not in (None, ""):
                    return str(value)[:500]
        return ""


@dataclass(frozen=True, slots=True)
class ToolCatalogEntry:
    tool: object
    metadata: VoiceToolCapability
    schema: dict[str, Any]
    description: str
    schema_fingerprint: str

    @property
    def name(self) -> str:
        return self.metadata.name

    def search_document(self) -> str:
        properties = self.schema.get("parameters", {}).get("properties", {})
        property_text = " ".join(
            name for name, value in properties.items() if isinstance(value, dict)
        )
        return " ".join(
            (
                self.name.replace("_", " "),
                self.metadata.capability.value.replace("_", " "),
                self.metadata.namespace.replace(".", " "),
                _retrieval_description(self.description),
                property_text,
                " ".join(sorted(self.metadata.required_fields)),
            )
        )


@dataclass(frozen=True, slots=True)
class EligibilityContext:
    surface: VoiceSurface
    authenticated: bool
    connector_states: Mapping[str, bool | None]
    fresh_frame_available: bool
    enabled_feature_rollouts: frozenset[str]
    authorization_state: IntentAuthorizationState
    required_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SelectionContext:
    finalized_request: str
    active_objective: str
    screen_referent: str
    prior_clarification: str
    turn_index: int


@dataclass(frozen=True, slots=True)
class ToolSelection:
    tool_names: tuple[str, ...]
    primary_tool: str | None
    active_capability: Capability | None
    fingerprint: str
    reason_codes: tuple[str, ...]
    scores: tuple[tuple[str, float], ...]


class ToolCatalog:
    def __init__(
        self,
        entries: Mapping[str, ToolCatalogEntry],
        unregistered_names: Sequence[str] = (),
    ) -> None:
        self.entries = dict(entries)
        self.unregistered_names = tuple(sorted(set(unregistered_names)))
        payload = [
            (
                name,
                entry.schema_fingerprint,
                _metadata_fingerprint_payload(entry.metadata),
            )
            for name, entry in sorted(self.entries.items())
        ]
        self.fingerprint = _fingerprint(payload)

    @classmethod
    def from_livekit_tools(cls, tools: Sequence[object]) -> ToolCatalog:
        entries: dict[str, ToolCatalogEntry] = {}
        unregistered: list[str] = []
        for tool in tools:
            name = tool_name(tool)
            if not name:
                continue
            metadata = VOICE_TOOL_REGISTRY.get(name)
            if metadata is None:
                unregistered.append(name)
                continue
            schema = _schema_for_tool(tool, metadata)
            description = str(schema.get("description") or "").strip()
            entries[name] = ToolCatalogEntry(
                tool=tool,
                metadata=metadata,
                schema=schema,
                description=description,
                schema_fingerprint=_fingerprint(schema),
            )
        return cls(entries, unregistered)

    def deterministic_eligible(
        self,
        context: EligibilityContext,
        structurally_allowed: frozenset[str],
    ) -> tuple[list[ToolCatalogEntry], tuple[str, ...]]:
        eligible: list[ToolCatalogEntry] = []
        reasons: list[str] = []
        for name, entry in sorted(self.entries.items()):
            metadata = entry.metadata
            if name not in structurally_allowed:
                reasons.append(f"structural_blocked:{name}")
                continue
            if metadata.rollout_state is ToolRollout.DISABLED:
                reasons.append(f"rollout_disabled:{name}")
                continue
            if (
                metadata.rollout_state is ToolRollout.LIMITED
                and metadata.feature_rollout not in context.enabled_feature_rollouts
            ):
                reasons.append(f"feature_rollout_blocked:{name}")
                continue
            if not context.authenticated:
                reasons.append(f"authentication_required:{name}")
                continue
            if context.surface not in metadata.allowed_surfaces:
                reasons.append(f"surface_blocked:{name}")
                continue
            if metadata.requires_fresh_desktop_frame and not context.fresh_frame_available:
                reasons.append(f"fresh_frame_required:{name}")
                continue
            unavailable_connectors = [
                connector
                for connector in metadata.required_connectors
                if context.connector_states.get(connector) is not True
            ]
            if unavailable_connectors:
                reasons.append(
                    f"connector_required:{name}:{','.join(unavailable_connectors)}"
                )
                continue
            if (
                context.authorization_state is IntentAuthorizationState.CANCELLED
                and metadata.effect is not ToolEffect.READ
            ):
                reasons.append(f"authorization_cancelled:{name}")
                continue
            eligible.append(entry)
        return eligible, tuple(reasons)

    @staticmethod
    def _core_floor(
        eligible: Sequence[ToolCatalogEntry], *, reads_only: bool = False
    ) -> tuple[str, ...]:
        """The core tools this turn must expose no matter how the selector scored.

        Semantic narrowing keeps the voice model focused, but it must never be able to
        make Buddy look like it lost a capability: on a turn where BM25 found nothing,
        the model saw an empty tool list and told the user it had no reminder tool, 50
        minutes after the same account used that tool. Wording decides which tools are
        SUGGESTED, never which ones EXIST.

        Intersected with ``eligible``, so every structural rule still holds. A
        speculative (non-finalized) turn has already had its writes stripped upstream by
        derive_turn_policy, so the floor cannot smuggle a side effect into one.
        """
        return tuple(
            sorted(
                entry.name
                for entry in eligible
                if entry.name in CORE_TOOLS
                and not (reads_only and entry.metadata.effect is not ToolEffect.READ)
            )
        )

    def select(
        self,
        selection: SelectionContext,
        eligibility: EligibilityContext,
        structurally_allowed: frozenset[str],
        active_intent: ActiveIntentState,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> ToolSelection:
        active_intent.expire_if_needed(selection.turn_index)
        effective_eligibility = eligibility
        eligible, eligibility_reasons = self.deterministic_eligible(
            effective_eligibility, structurally_allowed
        )
        if not eligible:
            return ToolSelection(
                tool_names=(),
                primary_tool=None,
                active_capability=None,
                fingerprint=self.selection_fingerprint(()),
                reason_codes=eligibility_reasons + ("no_eligible_tools",),
                scores=(),
            )
        if effective_eligibility.required_tools:
            eligible_by_name = {entry.name: entry for entry in eligible}
            required_names = tuple(sorted(effective_eligibility.required_tools))
            if any(name not in eligible_by_name for name in required_names):
                return ToolSelection(
                    tool_names=(),
                    primary_tool=None,
                    active_capability=None,
                    fingerprint=self.selection_fingerprint(()),
                    reason_codes=eligibility_reasons
                    + ("required_tool_ineligible",),
                    scores=(),
                )
            primary = eligible_by_name[required_names[0]]
            return ToolSelection(
                tool_names=required_names,
                primary_tool=primary.name,
                active_capability=primary.metadata.capability,
                fingerprint=self.selection_fingerprint(required_names),
                reason_codes=eligibility_reasons
                + ("required_tool_bundle",),
                scores=tuple((name, 1.0) for name in required_names),
            )

        query = _selection_query(selection, active_intent)
        documents = [_tokenize(entry.search_document()) for entry in eligible]
        query_tokens = _tokenize(query)
        scores = _bm25_scores(query_tokens, documents)
        if not _requests_multiple_actions(selection.finalized_request):
            all_entries = sorted(self.entries.values(), key=lambda entry: entry.name)
            all_scores = _bm25_scores(
                query_tokens,
                [_tokenize(entry.search_document()) for entry in all_entries],
            )
            eligible_names = {entry.name for entry in eligible}
            top_eligible_score = max(
                (
                    score
                    for entry, score in zip(all_entries, all_scores)
                    if entry.name in eligible_names
                ),
                default=0.0,
            )
            top_ineligible_score = max(
                (
                    score
                    for entry, score in zip(all_entries, all_scores)
                    if entry.name not in eligible_names
                ),
                default=0.0,
            )
            if top_ineligible_score > max(top_eligible_score * 1.25, 0.0):
                floor = self._core_floor(eligible)
                return ToolSelection(
                    tool_names=floor,
                    primary_tool=None,
                    active_capability=None,
                    fingerprint=self.selection_fingerprint(floor),
                    reason_codes=eligibility_reasons
                    + ("higher_scoring_tool_ineligible", "core_floor_applied"),
                    scores=(),
                )
        if active_intent.active_capability is not None and active_intent.missing_arguments:
            scores = [
                score + (8.0 if entry.metadata.capability == active_intent.active_capability else 0)
                for entry, score in zip(eligible, scores)
            ]
        if not active_intent.referenced_object:
            scores = [
                score
                - (
                    2.0
                    if any(name.endswith("_id") for name in entry.metadata.required_fields)
                    else 0.0
                )
                for entry, score in zip(eligible, scores)
            ]
        ranked = sorted(
            zip(eligible, scores),
            key=lambda row: (-row[1], row[0].name),
        )
        top_score = ranked[0][1]
        if top_score <= 0:
            floor = self._core_floor(eligible)
            return ToolSelection(
                tool_names=floor,
                primary_tool=None,
                active_capability=None,
                fingerprint=self.selection_fingerprint(floor),
                reason_codes=eligibility_reasons
                + ("no_semantic_match", "core_floor_applied"),
                scores=tuple((entry.name, round(score, 4)) for entry, score in ranked),
            )

        primary = ranked[0][0]
        action_clauses = _action_clauses(selection.finalized_request)
        if len(action_clauses) > 1:
            clause_scores = _bm25_scores(_tokenize(action_clauses[-1]), documents)
            if not active_intent.referenced_object:
                clause_scores = [
                    score
                    - (
                        2.0
                        if any(
                            name.endswith("_id")
                            for name in entry.metadata.required_fields
                        )
                        else 0.0
                    )
                    for entry, score in zip(eligible, clause_scores)
                ]
            clause_ranked = sorted(
                zip(eligible, clause_scores),
                key=lambda row: (-row[1], row[0].name),
            )
            if clause_ranked[0][1] > 0:
                primary = clause_ranked[0][0]
        chosen: list[ToolCatalogEntry] = []
        chosen_names: set[str] = set()

        def _choose(entry: ToolCatalogEntry) -> None:
            if entry.name not in chosen_names and len(chosen) < max_results:
                chosen.append(entry)
                chosen_names.add(entry.name)

        _choose(primary)
        for entry, _score in ranked:
            if entry.metadata.namespace == primary.metadata.namespace:
                _choose(entry)
        if _requests_multiple_actions(selection.finalized_request):
            secondary_namespaces: list[str] = []
            for entry, score in ranked:
                namespace = entry.metadata.namespace
                if namespace == primary.metadata.namespace:
                    continue
                if score < top_score * 0.55 or score <= 0:
                    continue
                if namespace not in secondary_namespaces:
                    secondary_namespaces.append(namespace)
                if len(secondary_namespaces) == 1:
                    break
            for entry, _score in ranked:
                if entry.metadata.namespace in secondary_namespaces:
                    _choose(entry)

        # Added AFTER max_results is spent, not competing for its slots, so the semantic
        # bundle is exactly what it would have been without the floor.
        floor = self._core_floor(eligible)
        selected_names = tuple(sorted(chosen_names | set(floor)))
        return ToolSelection(
            tool_names=selected_names,
            primary_tool=primary.name,
            active_capability=primary.metadata.capability,
            fingerprint=self.selection_fingerprint(selected_names),
            reason_codes=eligibility_reasons
            + (
                "core_floor_applied",
                f"semantic_bundle:{primary.metadata.namespace}",
            ),
            scores=tuple((entry.name, round(score, 4)) for entry, score in ranked),
        )

    def commit_selection(
        self,
        selection: ToolSelection,
        context: SelectionContext,
        active_intent: ActiveIntentState,
    ) -> None:
        if selection.primary_tool is None:
            return
        entry = self.entries.get(selection.primary_tool)
        if entry is None:
            return
        referenced_object = (
            context.screen_referent
            if entry.metadata.requires_fresh_desktop_frame
            or entry.metadata.namespace.startswith("desktop.")
            else active_intent.referenced_object
        )
        # The objective is always the turn that produced this selection. Appending to a
        # prior objective was only ever reached by matching correction words, and a
        # stale objective outliving its turn is what let one misread request keep
        # steering selection for the whole intent window.
        active_objective = context.finalized_request
        active_intent.begin(
            capability=entry.metadata.capability,
            tool_name_value=entry.name,
            required_arguments=(
                entry.metadata.required_fields - entry.metadata.empty_allowed_fields
            ),
            active_objective=active_objective,
            referenced_object=referenced_object,
            turn_index=context.turn_index,
            effect=entry.metadata.effect,
        )

    def selection_fingerprint(self, names: Sequence[str]) -> str:
        payload = [
            (
                name,
                self.entries[name].schema_fingerprint,
                _metadata_fingerprint_payload(self.entries[name].metadata),
            )
            for name in sorted(names)
            if name in self.entries
        ]
        return _fingerprint((TOOL_DISCOVERY_VERSION, self.fingerprint, payload))

    def metadata_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "name": entry.name,
                "capability": entry.metadata.capability.value,
                "namespace": entry.metadata.namespace,
                "effect": entry.metadata.effect.value,
                "surfaces": sorted(surface.value for surface in entry.metadata.allowed_surfaces),
                "prerequisites": sorted(value.value for value in entry.metadata.prerequisites),
                "connectors": sorted(entry.metadata.required_connectors),
                "risk": entry.metadata.risk.value,
                "required_arguments": sorted(entry.metadata.required_fields),
                "latency": entry.metadata.latency.value,
                "concurrency": entry.metadata.concurrency.value,
                "version": entry.metadata.version,
                "rollout_state": entry.metadata.rollout_state.value,
                "feature_rollout": entry.metadata.feature_rollout,
                "schema_fingerprint": entry.schema_fingerprint,
            }
            for entry in sorted(self.entries.values(), key=lambda value: value.name)
        ]


def recent_dialogue_context(
    chat_ctx: object,
    current_message_id: str,
) -> tuple[str, str]:
    """Return the prior assistant turn and any screen referent for tool selection.

    Prior USER turns are deliberately not returned. The only thing that ever selected
    among them was a list of correction words, and re-feeding arbitrary earlier turns
    into the retrieval query pollutes it instead.
    """
    del current_message_id
    prior_assistant = ""
    screen_referent = ""
    for item in getattr(chat_ctx, "items", []):
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        text = getattr(item, "text_content", "") or ""
        if role == "assistant" and text.strip():
            prior_assistant = text.strip()
        elif role == "system" and isinstance(content, list):
            for part in content:
                if isinstance(part, str) and "<screen_ui_context>" in part:
                    screen_referent = part.split("</screen_ui_context>", 1)[0]
    return prior_assistant[:500], screen_referent[-1500:]


def _schema_for_tool(
    tool: object,
    metadata: VoiceToolCapability,
) -> dict[str, Any]:
    info = getattr(tool, "info", None)
    raw_schema = getattr(info, "raw_schema", None)
    if isinstance(raw_schema, dict):
        return json.loads(json.dumps(raw_schema, sort_keys=True, default=str))
    canonical = tool_definition(metadata.name)
    if canonical is not None:
        return {
            "name": canonical["name"],
            "description": canonical["description"],
            "parameters": canonical["inputSchema"],
            "strict": canonical.get("strict") is True,
        }
    description = str(getattr(info, "description", "") or "")
    return {
        "name": metadata.name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {},
            "required": sorted(metadata.required_fields),
        },
    }


def _selection_query(
    selection: SelectionContext,
    active_intent: ActiveIntentState,
) -> str:
    # Continuity used to be gated on the turn matching a correction or follow-up
    # word list. It is gated on state instead, in two independent pieces.
    #
    # Buddy's own last question always counts. An answer to "when should I remind
    # you?" is "tonight", which names no tool on its own; scoring it against the
    # question is what keeps the answer on the tool that asked. This does not
    # depend on the user having phrased the answer any particular way, which is
    # exactly what the word list was failing to guess.
    continuity_parts: list[str] = [selection.prior_clarification]
    # The rest is intent-derived, so it is only meaningful while one is open.
    if active_intent.active_capability is not None:
        continuity_parts.append(selection.active_objective)
        continuity_parts.append(active_intent.active_capability.value)
        continuity_parts.append(active_intent.referenced_object)
    return " ".join(
        part
        for part in (
            selection.finalized_request,
            selection.finalized_request,
            *continuity_parts,
            selection.screen_referent,
        )
        if part
    )


def _requests_multiple_actions(text: str) -> bool:
    return len(_action_clauses(text)) > 1


def _action_clauses(text: str) -> tuple[str, ...]:
    clauses = tuple(
        clause.strip()
        for clause in re.split(r"\b(?:and then|and|then|also)\b", text, flags=re.I)
        if clause.strip()
    )
    return clauses or (text,)


def _tokenize(value: str) -> list[str]:
    tokens = _TOKEN_PATTERN.findall(value.casefold())
    normalized: list[str] = []
    for token in tokens:
        if token in _SEARCH_STOP_WORDS:
            continue
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("ing"):
            token = token[:-3]
        elif len(token) > 5 and token.endswith("er"):
            token = token[:-2]
        elif len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        normalized.append(token)
    return normalized


def _retrieval_description(description: str) -> str:
    positive_sentences: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", description):
        normalized = sentence.casefold()
        if any(
            marker in normalized
            for marker in ("do not", "don't", "never ", "not for", "instead")
        ):
            continue
        positive_sentences.append(sentence)
        if len(positive_sentences) == 2:
            break
    return " ".join(positive_sentences)


def _bm25_scores(query: Sequence[str], documents: Sequence[Sequence[str]]) -> list[float]:
    if not query or not documents:
        return [0.0 for _ in documents]
    document_count = len(documents)
    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(set(document))
    query_counts = Counter(query)
    scores: list[float] = []
    for document in documents:
        frequencies = Counter(document)
        score = 0.0
        for token, query_count in query_counts.items():
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            frequency_count = document_frequency[token]
            inverse_frequency = math.log(
                1.0 + (document_count - frequency_count + 0.5) / (frequency_count + 0.5)
            )
            denominator = frequency + 1.5
            score += inverse_frequency * (frequency * 2.5 / denominator) * query_count
        scores.append(score)
    return scores


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _metadata_fingerprint_payload(metadata: VoiceToolCapability) -> dict[str, Any]:
    return {
        "capability": metadata.capability.value,
        "namespace": metadata.namespace,
        "effect": metadata.effect.value,
        "surfaces": sorted(surface.value for surface in metadata.allowed_surfaces),
        "prerequisites": sorted(value.value for value in metadata.prerequisites),
        "connectors": sorted(metadata.required_connectors),
        "risk": metadata.risk.value,
        "required_fields": sorted(metadata.required_fields),
        "empty_allowed_fields": sorted(metadata.empty_allowed_fields),
        "latency": metadata.latency.value,
        "concurrency": metadata.concurrency.value,
        "version": metadata.version,
        "rollout_state": metadata.rollout_state.value,
        "feature_rollout": metadata.feature_rollout,
    }
