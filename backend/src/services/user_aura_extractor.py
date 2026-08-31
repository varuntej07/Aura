"""
UserAuraExtractor — passive behavioral profile builder.

Fires as a fire-and-forget asyncio task after every chat message. Reads the user's
previous query from UserAura/{uid}, extracts behavioral and interest signals from the
current message via Gemini Flash, and merges the result into the UserAura document.

Never blocks the chat response stream. All failures are logged and swallowed.

Firestore path: UserAura/{uid}
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

from google.cloud import firestore as fs
from pydantic import BaseModel, ValidationError, field_validator

from ..config.settings import settings
from ..lib.logger import logger
from ..prompts import USER_AURA_EXTRACTION_SYSTEM_PROMPT, user_aura_extraction_user_prompt
from .life_facts_schema import (
    LIFE_FACT_DESCRIPTIONS,
    LIFE_FACT_KEYS,
    LIFE_FACTS_FIELD,
    apply_life_fact,
    remove_life_fact,
)
from .memory.atom_store import AtomInput, upsert_atoms
from .memory.fields import ATOM_TYPE_FACT, ATOM_TYPE_INTEREST_SUBJECT
from .model_provider import get_model_provider
from .user_aura_schema import (
    CATEGORY_LABELS,
    DEAD_INTEREST_FIELDS,
    INTEREST_CATEGORIES,
    LEGACY_SUNSET_CATEGORY_COUNT,
    OTHER_CATEGORY,
    apply_interest_signal,
    category_count,
)
from .user_aura_schema import sanitize_firestore_key as _sanitize_firestore_key

_MAX_INFERRED_GOALS = 10
_MAX_EXPLICIT_FACTS = 20          # cap on stored durable facts per user

# Low temperature: we want consistent structured JSON, not creative output.
_EXTRACTION_TEMPERATURE = 0.1

_MIN_DIRECTIVE_HINT_LENGTH = 15   # hints shorter than this are too vague to be actionable
_MAX_ACCEPTED_HINTS = 30          # cap on stored accepted hints per user
_MAX_STYLE_SIGNALS = 10           # cap on style avoid/prefer entries in UserAura
_MAX_INTERESTS_PER_MESSAGE = 3    # categories the model may emit per message
_MAX_LIFE_FACTS_PER_MESSAGE = 3   # durable life facts the model may emit per message

# Firestore hard-fails a document write at 1 MiB. Warn well before so a bloating
# profile screams in logs instead of silently freezing on a swallowed write.
_PROFILE_SIZE_WARN_BYTES = 800_000


class InterestSignal(BaseModel):
    """One interest extracted from a message: a canonical category plus the
    specific subject named in it. Subject is what gives personalization its edge
    — "KCR" under politics_governance, not just "politics"."""

    category: str             # one of user_aura_schema.INTEREST_CATEGORIES
    # Specific person/place/org/product/topic, or null. Defaulted so a model
    # response that omits the key degrades to category-only instead of failing
    # validation and dropping the whole extraction.
    subject: str | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_known_category(cls, value: object) -> str:
        # The model is constrained by the prompt, but never trust it: any value
        # outside the taxonomy collapses to OTHER so the closed-set contract holds.
        slug = str(value or "").strip().lower().replace(" ", "_").replace("&", "")
        slug = "_".join(part for part in slug.split("_") if part)
        return slug if slug in INTEREST_CATEGORIES else OTHER_CATEGORY


class LifeFactSignal(BaseModel):
    """One durable life fact extracted from a message: a closed-taxonomy key plus
    the concrete value named (e.g. key="has_pet", value="dog named Bruno"). These
    arm the Icebreaker engine's life-aware openers. Off-list keys are dropped by
    the schema writer, so the closed-set contract holds even if the model invents
    a key."""

    key: str                  # one of life_facts_schema.LIFE_FACT_KEYS
    value: str | None = None  # the concrete value (pet name, city, ...), or null
    negated: bool = False     # True when the user DENIES/corrects this fact, so it is cleared


class MessageInsight(BaseModel):
    # Request classification
    primary_intent: str | None     # task_request | seeking_advice | information_lookup |
                                   # casual_chat | venting | complaint | gratitude | follow_up_only
                                   # null on zero-signal/ack messages (the LLM omits an intent)
    secondary_intent: str | None

    # Interest extraction — category (closed taxonomy) + specific subject. Max 3.
    interests: list[InterestSignal]

    # Durable life facts (closed-taxonomy key + value) that arm life-aware
    # notifications. Sparse by design — usually empty. Defaulted so a model that
    # omits the key degrades to "no facts" instead of failing the whole extraction.
    life_facts: list[LifeFactSignal] = []

    # Domain and behavioral signals — required enums, but the LLM can still omit them
    # (null) on zero-signal/ack messages, so they accept None to avoid rejecting the
    # whole extraction. Every reader guards for None before use.
    domain: str | None             # work | health | finance | learning | social |
                                   # entertainment | personal | technical | unclear
    tone: str | None               # casual | terse | verbose | formal | playful
    emotional_state: str | None    # neutral | anxious | frustrated | excited | anticipatory |
                                   # curious | sad — null if not clearly signaled
    urgency: str | None            # none | low | medium | high

    # Interaction preference signals
    response_depth_preference: str | None   # wants_brief | wants_detailed | wants_step_by_step |
                                            # wants_examples | wants_opinion — null if not signaled
    question_type: str | None      # how_to | what_is | opinion_request | recommendation |
                                   # comparison | troubleshooting — null if not applicable

    # Identity signals
    explicit_facts: list[str]      # durable identity/preference facts only — e.g. "I live in Hyderabad",
                                   # "dislikes early-morning showers". NOT task params like reminder
                                   # times, dates, deadlines, or one-off scheduling details.
    inferred_goal_hints: list[str] # high-confidence goal inferences only — max 3

    # Metadata
    used_prev_query_context: bool  # True if the LLM used prev_query to resolve ambiguity
    extraction_skipped: bool       # True only for zero-signal messages (pure acks)

    # Turn scoring - evaluates Buddy's previous response quality using the current message as signal
    turn_score: int                # 1 (positive), -1 (negative), 0 (no prior response to score)
    signal_type: Literal[
        "re_query", "correction", "clarification", "acknowledgement", "praise", "none"
    ]
    directive_hint: str | None     # populated only for correction or re_query with a concrete instruction


_CATEGORY_REFERENCE = "\n".join(
    f" - {slug}: {label}" for slug, label in CATEGORY_LABELS.items()
)

_LIFE_FACT_REFERENCE = "\n".join(
    f" - {key}: {LIFE_FACT_DESCRIPTIONS[key]}" for key in LIFE_FACT_KEYS
)

def _argmax(freq_map: dict[str, int]) -> str | None:
    return max(freq_map, key=lambda k: freq_map[k]) if freq_map else None


def _merge_profile(
    existing: dict[str, Any],
    insight: MessageInsight,
    current_message: str,
) -> dict[str, Any]:
    """
    Produce the updated UserAura document from the existing profile and a new insight.
    Pure function — no I/O. The caller writes the result to Firestore.
    """
    profile: dict[str, Any] = dict(existing)
    now = datetime.now(UTC)

    # Always advance the previous query pointer and timestamp regardless of skip.
    profile["prev_user_query"] = current_message
    profile["last_updated"] = now.isoformat()

    if insight.extraction_skipped:
        return profile

    def _inc(map_key: str, field: str) -> None:
        freq_map: dict[str, int] = profile.setdefault(map_key, {})
        safe = _sanitize_firestore_key(field)
        freq_map[safe] = freq_map.get(safe, 0) + 1

    # Intents
    primary_intent = insight.primary_intent
    if primary_intent:
        _inc("intent_distribution", primary_intent)
    if insight.secondary_intent:
        _inc("intent_distribution", insight.secondary_intent)

    # Interests — canonical category + specific subject, time-decayed. Replaces the
    # old free-text deep_interest/surface_topic/named_entity frequency maps.
    interests = profile.get("interests")
    if not isinstance(interests, dict):
        interests = {}
        profile["interests"] = interests
    for signal in insight.interests[:_MAX_INTERESTS_PER_MESSAGE]:
        apply_interest_signal(interests, signal.category, signal.subject, now)

    # Life facts — the sparse, typed map that arms life-aware notifications.
    # The schema writer silently drops off-taxonomy keys, so the closed-set holds.
    if insight.life_facts:
        life_facts = profile.get(LIFE_FACTS_FIELD)
        if not isinstance(life_facts, dict):
            life_facts = {}
            profile[LIFE_FACTS_FIELD] = life_facts
        for fact in insight.life_facts[:_MAX_LIFE_FACTS_PER_MESSAGE]:
            if fact.negated:
                remove_life_fact(life_facts, fact.key)
            else:
                apply_life_fact(life_facts, fact.key, fact.value, now)

    # Domain, tone, urgency
    if insight.domain:
        _inc("domain_frequencies", insight.domain)
    if insight.tone:
        _inc("tone_signals", insight.tone)
    if insight.urgency and insight.urgency != "none":
        _inc("urgency_distribution", insight.urgency)

    # Optional signals
    if insight.emotional_state:
        _inc("emotional_signals", insight.emotional_state)
    if insight.question_type:
        _inc("question_type_distribution", insight.question_type)
    if insight.response_depth_preference:
        _inc("depth_preference_signals", insight.response_depth_preference)

    # Lists — append with dedup (order-preserving, oldest entries kept)
    facts: list[str] = profile.setdefault("explicit_facts", [])
    for fact in insight.explicit_facts:
        if fact not in facts:
            facts.append(fact)
    # Cap durable facts — keep the most recent when over the limit.
    if len(facts) > _MAX_EXPLICIT_FACTS:
        profile["explicit_facts"] = facts[-_MAX_EXPLICIT_FACTS:]

    goals: list[str] = profile.setdefault("inferred_goals", [])
    for goal in insight.inferred_goal_hints:
        if goal not in goals:
            goals.append(goal)
    # Keep the most recent goals when over cap — older ones are likely stale.
    if len(goals) > _MAX_INFERRED_GOALS:
        profile["inferred_goals"] = goals[-_MAX_INFERRED_GOALS:]

    # Computed dominant values — recalculated after every merge so they stay current.
    profile["dominant_tone"] = _argmax(profile.get("tone_signals", {}))
    profile["response_depth_preference"] = _argmax(profile.get("depth_preference_signals", {}))
    profile["extraction_count"] = profile.get("extraction_count", 0) + 1

    # Sunset the DEAD interest maps (nothing reads them) once the new structure is
    # mature, to reclaim doc space. deep_interest_frequencies is intentionally kept
    # — the shipped Flutter app still reads it and the accessors fall back to it —
    # until the app update that reads `interests` has reached every client.
    if category_count(profile) >= LEGACY_SUNSET_CATEGORY_COUNT:
        for dead_field in DEAD_INTEREST_FIELDS:
            profile.pop(dead_field, None)

    return profile


async def _read_user_aura_profile(uid: str) -> dict[str, Any]:
    from .firebase import admin_firestore

    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(uid).get()
        return snap.to_dict() or {}

    return await asyncio.to_thread(_fetch)


async def _merge_and_write_user_aura(uid: str, insight: MessageInsight, message: str) -> dict[str, Any]:
    """Transactionally read the CURRENT profile, fold in this turn's insight, and write.

    The read-modify-write runs inside a Firestore transaction so it cannot lose a
    concurrent write. Two rapid capture turns, or a per-session reflection write landing
    in between, each cause the transaction to retry: it re-reads fresh state and re-applies
    _merge_profile, which only touches capture-owned fields and copies everything else
    (including the reflection tier's storylines / traits) straight through. The LLM call
    has already happened, so the transaction body is pure and fast.
    """
    from .firebase import admin_firestore

    db = admin_firestore()
    ref = db.collection("UserAura").document(uid)

    def _txn() -> dict[str, Any]:
        transaction = db.transaction()

        @fs.transactional
        def _apply(tx: fs.Transaction) -> dict[str, Any]:
            snap = ref.get(transaction=tx)
            fresh = snap.to_dict() or {}
            updated = _merge_profile(fresh, insight, message)
            tx.set(ref, updated)
            return updated

        return _apply(transaction)

    updated = await asyncio.to_thread(_txn)

    # Firestore hard-fails a write above 1 MiB and that failure is swallowed downstream,
    # which would silently freeze the profile. Warn loudly while there is still headroom.
    approx_bytes = len(json.dumps(updated, default=str).encode("utf-8"))
    if approx_bytes >= _PROFILE_SIZE_WARN_BYTES:
        logger.warn("UserAuraExtractor: profile approaching Firestore 1MB limit", {
            "user_id": uid,
            "approx_bytes": approx_bytes,
            "interest_categories": category_count(updated),
        })
    return updated


def _derive_style_signal_description(
    signal_type: str,
    directive_hint: str | None,
    score: int,
) -> str:
    if directive_hint and len(directive_hint) <= 80:
        return directive_hint
    negative_descriptions: dict[str, str] = {
        "re_query":      "response that required the user to repeat their question",
        "correction":    "response with incorrect or incomplete information",
        "clarification": "response that required follow-up clarification",
    }
    positive_descriptions: dict[str, str] = {
        "acknowledgement": "clear and directly actionable response",
        "praise":          "response the user found exactly right",
    }
    if score == -1:
        return negative_descriptions.get(signal_type, "unhelpful response pattern")
    return positive_descriptions.get(signal_type, "response the user found helpful")


async def _write_turn_signal_to_firestore(
    uid: str,
    session_id: str | None,
    insight: MessageInsight,
    current_message: str,
    prev_buddy_response: str,
) -> None:
    from .firebase import admin_firestore

    turn_id = str(uuid.uuid4())
    document = {
        "turn_id": turn_id,
        "session_id": session_id or "unknown",
        "timestamp": datetime.now(UTC).isoformat(),
        "buddy_response_snippet": prev_buddy_response[:300],
        "next_state_snippet": current_message[:300],
        "score": insight.turn_score,
        "signal_type": insight.signal_type,
        "hint": insight.directive_hint,
    }

    def _put_turn() -> None:
        (
            admin_firestore()
            .collection("UserSignals")
            .document(uid)
            .collection("turns")
            .document(turn_id)
            .set(document)
        )

    await asyncio.to_thread(_put_turn)
    logger.info("UserAuraExtractor: turn signal written", {
        "user_id": uid,
        "turn_id": turn_id,
        "score": insight.turn_score,
        "signal_type": insight.signal_type,
        "has_directive_hint": insight.directive_hint is not None,
        "session_id": session_id or "unknown",
    })


async def _write_accepted_hint_with_cap(
    uid: str,
    session_id: str | None,
    hint: str,
) -> None:
    from .firebase import admin_firestore

    timestamp = datetime.now(UTC).isoformat()

    def _put_hint() -> bool:
        db = admin_firestore()
        hints_ref = db.collection("UserSignals").document(uid).collection("accepted_hints")
        existing = list(hints_ref.order_by("timestamp").limit(_MAX_ACCEPTED_HINTS).stream())
        cap_hit = len(existing) >= _MAX_ACCEPTED_HINTS
        if cap_hit:
            existing[0].reference.delete()
        hints_ref.document().set({
            "hint": hint,
            "timestamp": timestamp,
            "session_id": session_id or "unknown",
        })
        return cap_hit

    cap_hit = await asyncio.to_thread(_put_hint)
    logger.info("UserAuraExtractor: accepted hint written", {
        "user_id": uid,
        "hint_preview": hint[:60],
        "oldest_deleted_for_cap": cap_hit,
        "session_id": session_id or "unknown",
    })


async def _update_user_aura_style_signals(
    uid: str,
    score: int,
    signal_type: str,
    directive_hint: str | None,
) -> None:
    from .firebase import admin_firestore

    description = _derive_style_signal_description(signal_type, directive_hint, score)
    field = "response_style_avoid" if score == -1 else "response_style_prefer"

    def _update() -> str:
        db = admin_firestore()
        ref = db.collection("UserAura").document(uid)
        data = (ref.get().to_dict()) or {}
        signals: list[str] = list(data.get(field, []))
        if description in signals:
            return "duplicate_skipped"
        signals.append(description)
        trimmed = len(signals) > _MAX_STYLE_SIGNALS
        if trimmed:
            signals = signals[-_MAX_STYLE_SIGNALS:]
        ref.set({field: signals}, merge=True)
        return "added_and_trimmed" if trimmed else "added"

    status = await asyncio.to_thread(_update)
    logger.info("UserAuraExtractor: style signal updated", {
        "user_id": uid,
        "field": field,
        "description_preview": description[:60],
        "status": status,
    })


async def _user_has_granted_aura_consent(
    uid: str, user_doc: dict[str, Any] | None = None,
) -> bool:
    """Read aura_consent_granted from users/{uid}. Returns False on any error (safe
    default). ``user_doc`` lets a caller that already fetched the doc this turn
    (the chat handler) pass it through instead of a redundant re-fetch -- this
    function is called from two independent fire-and-forget tasks per chat turn
    (the aura extractor and intent-sense), so without this it re-read the same doc
    twice on top of the chat handler's own reads (see firestore_read_audit_20260706
    memory)."""
    if user_doc is not None:
        return user_doc.get("aura_consent_granted", False) is True

    from .firebase import admin_firestore

    def _fetch() -> bool:
        snap = admin_firestore().collection("users").document(uid).get()
        if not snap.exists:
            return False
        return (snap.to_dict() or {}).get("aura_consent_granted", False) is True

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("UserAuraExtractor: consent check failed, skipping extraction", {
            "user_id": uid,
            "error": str(exc),
        })
        return False


# Batched consent lookup for per-user fan-outs, with a short TTL cache.
#
# Consent changes on the timescale of onboarding, not minutes, and the hourly tick
# fan-out asks about every active user at once. Cached and batched for the same reason
# fcm_token_registry caches the active-user scan. Modelled on that TTL deliberately.
_CONSENT_CACHE_TTL_SECONDS = 180
_consent_cache: dict[str, tuple[bool, float]] = {}


async def consented_user_ids(uids: list[str]) -> set[str]:
    """The subset of ``uids`` with aura_consent_granted == True.

    RAISES on a read failure rather than returning a partial set. Callers use this to
    SKIP work, so a silent partial answer would silently suppress real users; the caller
    is expected to catch and fall back to treating everyone as consenting. One batched
    ``get_all`` plus a short TTL cache, so an hourly fan-out over N users is one round
    trip and, on a warm cache, zero reads.
    """
    now = time.monotonic()
    resolved: set[str] = set()
    missing: list[str] = []
    for uid in uids:
        cached = _consent_cache.get(uid)
        if cached is not None and (now - cached[1]) < _CONSENT_CACHE_TTL_SECONDS:
            if cached[0]:
                resolved.add(uid)
        else:
            missing.append(uid)
    if not missing:
        return resolved

    from .firebase import admin_firestore

    def _fetch() -> dict[str, bool]:
        database = admin_firestore()
        refs = [database.collection("users").document(uid) for uid in missing]
        out: dict[str, bool] = {}
        for snap in database.get_all(refs):
            granted = bool(
                snap.exists and (snap.to_dict() or {}).get("aura_consent_granted", False) is True
            )
            out[snap.id] = granted
        return out

    fetched = await asyncio.to_thread(_fetch)
    stamped = time.monotonic()
    for uid in missing:
        granted = fetched.get(uid, False)
        _consent_cache[uid] = (granted, stamped)
        if granted:
            resolved.add(uid)
    return resolved


def _insight_entity_keys(insight: MessageInsight) -> list[str]:
    """Entity keys captured from the already-paid extraction result."""
    keys: list[str] = []
    for signal in insight.interests or []:
        if signal.subject and signal.subject.strip():
            keys.append(signal.subject.strip())
    for fact in insight.life_facts or []:
        if fact.value and fact.value.strip() and not fact.negated:
            keys.append(fact.value.strip())
    return keys


async def _write_graph_turn_provenance(
    uid: str,
    insight: MessageInsight,
    message: str,
    session_id: str | None,
    turn_id: str | None,
    turn_index: int | None,
    surface: str,
) -> None:
    if not session_id:
        return
    resolved_turn_id = turn_id or str(uuid.uuid4())
    from .session_followup.lifecycle import session_lifecycle_service

    await session_lifecycle_service.note_user_turn(
        uid,
        session_id,
        surface=surface,
        turn_id=resolved_turn_id,
        turn_index=max(0, int(turn_index or 0)),
        text=message,
        entity_keys=_insight_entity_keys(insight),
    )
    # The old GRAPH_BUILD-gated `write_turn_provenance` call that lived here was
    # dead code: the symbol never existed in memory.graph_store, and the flag
    # being off hid the broken import. Removed 2026-07-20 when the graph went
    # always-on. Turn-level session notes are `note_user_turn` above; graph
    # content itself is written by `_upsert_graph_from_insight` below.


async def _upsert_graph_from_insight(uid: str, insight: MessageInsight) -> None:
    try:
        from .memory.graph_store import (
            GraphEdgeInput,
            atom_node,
            entity_node,
            upsert_graph,
        )

        nodes = []
        edges = []
        for fact in insight.explicit_facts or []:
            if isinstance(fact, str) and fact.strip():
                nodes.append(atom_node(ATOM_TYPE_FACT, fact.strip(), weight=0.6))
        for signal in insight.interests or []:
            subject = (signal.subject or "").strip()
            if not subject:
                continue
            subject_atom = atom_node(
                ATOM_TYPE_INTEREST_SUBJECT,
                subject,
                weight=0.4,
            )
            subject_entity = entity_node(subject)
            nodes.extend((subject_atom, subject_entity))
            edges.append(GraphEdgeInput(
                subject_atom.node_id, subject_entity.node_id, "about",
            ))
            if signal.category:
                category_entity = entity_node(signal.category)
                nodes.append(category_entity)
                edges.append(GraphEdgeInput(
                    subject_entity.node_id,
                    category_entity.node_id,
                    "categorized_as",
                ))
        if nodes or edges:
            await upsert_graph(uid, nodes, edges, source="extractor")
    except Exception as exc:
        logger.warn("UserAuraExtractor: graph build failed", {
            "user_id": uid,
            "error": str(exc),
        })


async def _upsert_memory_atoms_from_insight(
    uid: str,
    insight: MessageInsight,
    *,
    message: str,
    session_id: str | None,
    turn_id: str | None,
) -> None:
    """Persist this turn's durable facts and named interest subjects into the UNBOUNDED
    long-term memory store (services/memory), so they are recallable by semantic
    similarity forever, independent of the capped UserAura digest. Captured at extraction
    time (pre-cap) so nothing the user told us is ever lost to recall. Fire-and-forget:
    upsert_atoms swallows its own errors and never blocks the chat path."""
    atoms: list[AtomInput] = []
    for fact in insight.explicit_facts or []:
        if isinstance(fact, str) and fact.strip():
            atoms.append(AtomInput(
                text=fact.strip(),
                atom_type=ATOM_TYPE_FACT,
                importance=0.6,
                confidence=0.9,
                confirmation_status="explicit_user",
                source_conversation_id=session_id or "",
                source_message_id=turn_id or "",
                evidence_text=message,
            ))
    for signal in insight.interests or []:
        subject = (signal.subject or "").strip()
        if subject:
            atoms.append(AtomInput(
                text=subject,
                atom_type=ATOM_TYPE_INTEREST_SUBJECT,
                importance=0.4,
                categories=[signal.category] if signal.category else [],
                confidence=0.8,
                confirmation_status="explicit_user",
                source_conversation_id=session_id or "",
                source_message_id=turn_id or "",
                evidence_text=message,
            ))
    if atoms:
        await upsert_atoms(uid, atoms, source="extractor")
    await _upsert_graph_from_insight(uid, insight)


async def extract_and_update_user_aura(
    uid: str,
    message: str,
    session_id: str | None = None,
    prev_buddy_response: str | None = None,
    user_doc: dict[str, Any] | None = None,
    turn_id: str | None = None,
    turn_index: int | None = None,
    surface: str = "chat",
) -> None:
    """
    Public entry point. Called via asyncio.create_task from the chat handler.

    Flow:
      0. Consent check — skip entirely if the user has not granted Aura consent.
      1. Read UserAura/{uid} -- retrieves prev_user_query and current profile.
      2. Build extraction prompt with current message + prev_user_query + prev_buddy_response.
      3. Gemini Flash extracts a MessageInsight including profile signals and turn scoring.
      4. Merge insight into the profile and write back.
      5. If prev_buddy_response is available, log the turn signal and run feedback loop updates.

    ``user_doc`` lets the chat handler pass the users/{uid} doc it already fetched
    this turn instead of this (detached, fire-and-forget) task re-fetching it.

    All exceptions are caught and logged. This function never raises.
    """
    started_at = time.monotonic()
    consent_ms = 0
    profile_read_ms = 0
    model_ms = 0
    provenance_write_ms = 0
    profile_merge_ms = 0
    memory_mirror_ms = 0
    # Step 0: GDPR consent gate. Skip if the user has not opted in, but log it so a
    # frozen profile (a user actively chatting whose Aura never updates) shows up in
    # logs instead of looking identical to "healthy and quiet". This skip being
    # silent is exactly what hid a 5-week profile freeze. The check reads
    # users/{uid}.aura_consent_granted, written at onboarding and by the memory toggle.
    stage_started = time.monotonic()
    has_consent = await _user_has_granted_aura_consent(uid, user_doc)
    consent_ms = round((time.monotonic() - stage_started) * 1000)
    if not has_consent:
        logger.info("UserAuraExtractor: extraction skipped, Aura consent not granted", {
            "user_id": uid,
            "surface": surface,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "consent_ms": consent_ms,
        })
        return

    insight: MessageInsight | None = None
    try:
        stage_started = time.monotonic()
        profile = await _read_user_aura_profile(uid)
        profile_read_ms = round((time.monotonic() - stage_started) * 1000)
        prev_query: str | None = profile.get("prev_user_query")

        prompt = user_aura_extraction_user_prompt(
            message=message,
            previous_query=prev_query or "",
            previous_response=prev_buddy_response or "",
        )
        stage_started = time.monotonic()
        insight = cast(MessageInsight, await get_model_provider().cheap(
            prompt,
            system=USER_AURA_EXTRACTION_SYSTEM_PROMPT,
            response_model=MessageInsight,
            temperature=_EXTRACTION_TEMPERATURE,
            model=settings.TIER_EXTRACTION,
        ))
        model_ms = round((time.monotonic() - stage_started) * 1000)

        # Provenance is one immutable document per turn. It is captured from the
        # extraction result before the eventually-consistent graph write below.
        stage_started = time.monotonic()
        await _write_graph_turn_provenance(
            uid, insight, message, session_id, turn_id, turn_index, surface,
        )
        provenance_write_ms = round((time.monotonic() - stage_started) * 1000)
        stage_started = time.monotonic()
        updated = await _merge_and_write_user_aura(uid, insight, message)
        profile_merge_ms = round((time.monotonic() - stage_started) * 1000)

        # Mirror the durable facts + named subjects into the unbounded long-term memory
        # store for query-relevant recall. Best-effort; never affects the merge above.
        stage_started = time.monotonic()
        await _upsert_memory_atoms_from_insight(
            uid,
            insight,
            message=message,
            session_id=session_id,
            turn_id=turn_id,
        )
        memory_mirror_ms = round((time.monotonic() - stage_started) * 1000)

        logger.info("UserAuraExtractor: profile updated", {
            "user_id": uid,
            "primary_intent": insight.primary_intent,
            "interests": [f"{s.category}:{s.subject}" for s in insight.interests],
            "domain": insight.domain,
            "extraction_skipped": insight.extraction_skipped,
            "used_prev_query": insight.used_prev_query_context,
            "extraction_count": updated.get("extraction_count"),
            "turn_score": insight.turn_score,
            "signal_type": insight.signal_type,
            "surface": surface,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "consent_ms": consent_ms,
            "profile_read_ms": profile_read_ms,
            "model_ms": model_ms,
            "provenance_write_ms": provenance_write_ms,
            "profile_merge_ms": profile_merge_ms,
            "memory_mirror_ms": memory_mirror_ms,
        })

    except ValidationError as exc:
        logger.warn("UserAuraExtractor: insight parse failed -- Gemini returned malformed JSON", {
            "user_id": uid,
            "error": str(exc),
            "surface": surface,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "model_ms": model_ms,
        })
    except Exception as exc:
        logger.warn("UserAuraExtractor: extraction failed", {
            "user_id": uid,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "surface": surface,
            "duration_ms": round((time.monotonic() - started_at) * 1000),
            "consent_ms": consent_ms,
            "profile_read_ms": profile_read_ms,
            "model_ms": model_ms,
            "provenance_write_ms": provenance_write_ms,
            "profile_merge_ms": profile_merge_ms,
            "memory_mirror_ms": memory_mirror_ms,
        })

    # Turn signal logging only makes sense when there is a previous response to score.
    # Skip entirely on the first message of a session.
    if insight is None or prev_buddy_response is None:
        return

    try:
        await _write_turn_signal_to_firestore(uid, session_id, insight, message, prev_buddy_response)
    except Exception as exc:
        logger.warn("UserAuraExtractor: turn signal write failed", {"user_id": uid, "error": str(exc)})

    accepted_hint = insight.directive_hint
    if (
        insight.signal_type in ("correction", "re_query")
        and accepted_hint is not None
        and len(accepted_hint) >= _MIN_DIRECTIVE_HINT_LENGTH
    ):
        try:
            await _write_accepted_hint_with_cap(uid, session_id, accepted_hint)
        except Exception as exc:
            logger.warn("UserAuraExtractor: accepted hint write failed", {"user_id": uid, "error": str(exc)})

    if insight.turn_score != 0:
        try:
            await _update_user_aura_style_signals(
                uid, insight.turn_score, insight.signal_type, insight.directive_hint
            )
        except Exception as exc:
            logger.warn("UserAuraExtractor: style signal update failed", {"user_id": uid, "error": str(exc)})
