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
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError, field_validator

from ..lib.logger import logger
from .life_facts_schema import (
    LIFE_FACT_DESCRIPTIONS,
    LIFE_FACT_KEYS,
    LIFE_FACTS_FIELD,
    apply_life_fact,
    remove_life_fact,
)
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

_EXTRACTION_SYSTEM_PROMPT = f"""\
            You are the insight extractor for a personal AI assistant called Aura.
            Everything you extract becomes the user's "UserAura" profile, which directly powers
            two things: (1) how Aura tailors its chat and voice replies to this user, and
            (2) which notifications we send them and how we word them. Accuracy here is the
            difference between generic and genuinely personal -- extract carefully.

            Rules:
            - Extract only signals you are highly confident about.
            - Use null for optional string fields and empty lists where there is no clear signal.
            - If you cannot confidently extract meaningful preferences from the current message alone,
            use the provided previous query as additional context. Set used_prev_query_context to true.
            - Always prefer the current message. Use the previous query only to resolve ambiguity or fill gaps.
            - Set extraction_skipped to true ONLY for pure acknowledgments with zero informational content
            such as standalone "ok", "thanks", "yes", "no", "sure", "got it" with nothing else attached.

            INTERESTS (the most important part):
            For each message produce up to 3 interest signals. Each signal has two parts:
              * category: map the message to EXACTLY ONE category from the fixed list below. If
                nothing fits, use "other". Never invent a category outside this list.
              * subject: the SPECIFIC person, place, organisation, product, event, or topic the user
                actually named. Specificity is the whole point -- if the user asks about KCR, return
                category "politics_governance" with subject "KCR", NOT just "politics", because knowing
                it is *KCR* lets us personalise ("KCR is in the news today") instead of a flat
                "politics update". Set subject to null only when the message has no concrete subject
                (e.g. "what does this word mean", "convert 5km to miles").

            Categories (slug: meaning):
{_CATEGORY_REFERENCE}

            Interest examples:

            "Who is the CM of Telangana right now?"
            interests: [{{"category": "politics_governance", "subject": "KCR"}},
                        {{"category": "regional_local_affairs", "subject": "Telangana"}}]
            primary_intent: information_lookup, domain: unclear

            "Give me a tweet for the RCB vs MI match"
            interests: [{{"category": "sports", "subject": "RCB vs MI"}},
                        {{"category": "technology_computing", "subject": "tweet writing"}}]
            primary_intent: task_request, domain: entertainment, tone: casual

            "Is Triton and CUDA the same but in different languages?"
            interests: [{{"category": "technology_computing", "subject": "CUDA"}}]
            question_type: comparison, domain: technical

            "What is the on-road price of the XUV 3XO?"
            interests: [{{"category": "automotive", "subject": "XUV 3XO"}}]
            primary_intent: information_lookup

            "explain SGEMM"
            interests: [{{"category": "technology_computing", "subject": "SGEMM"}}]
            domain: technical, question_type: what_is

            "convert 100 euros to rupees"
            interests: [{{"category": "personal_finance", "subject": null}}]
            primary_intent: information_lookup

            explicit_facts captures DURABLE facts about the user -- who they are and their stable
            preferences -- never transient task parameters. Keep identity, relationships, location,
            job, and standing likes/dislikes. Drop reminder times, dates, deadlines, and one-off
            scheduling details: those belong to the task being requested, not the user's profile.

            "remind me to take a shower tomorrow night, want it done before 4, after lunch around 1 PM"
            explicit_facts: []   (every detail here is a reminder parameter, not a fact about the user)
            primary_intent: task_request, domain: personal

            "I don't like taking a shower early in the morning"
            explicit_facts: ["dislikes showering early in the morning"]
            domain: personal

            "I just got rejected after 8 rounds of interviews, 6 hours of it in person"
            explicit_facts: ["was rejected after an 8-round interview that included a 6-hour onsite"]
            emotional_state: sad, domain: work

            LIFE FACTS (sparse, high-value — usually empty):
            Separately from explicit_facts, extract life_facts ONLY when the user states a
            durable fact that fits one of the fixed keys below. Each life fact is a (key, value)
            pair: pick EXACTLY ONE key from the list, and put the concrete detail in value.
            Emit a life fact only when you are highly confident and the detail is durable (not a
            one-off). Most messages produce NO life facts -- return an empty list then. Never
            invent a key outside this list.

            The fact MUST be about the user themselves, stated in the first person (their own
            pet, home, job, habit). NEVER store a fact about another person -- "my friend's dog",
            "my sister is a doctor", "my roommate has a cat" produce NO life fact. When unsure
            whose fact it is, emit nothing.

            If the user DENIES or corrects a fact ("I don't have a dog", "I never said I was
            married", "I moved out of Hyderabad"), emit that key with "negated": true and no
            value, so the stored fact is cleared. Use negated only for an explicit denial.

            Life fact keys (key: meaning):
{_LIFE_FACT_REFERENCE}

            Life fact examples:

            "my dog Bruno keeps chewing my shoes"
            life_facts: [{{"key": "has_pet", "value": "dog named Bruno"}}]

            "I'm based in Hyderabad and usually take the metro to work"
            life_facts: [{{"key": "home_city", "value": "Hyderabad"}},
                         {{"key": "commute_mode", "value": "takes the metro"}}]

            "what's a good protein intake" (no durable fact stated)
            life_facts: []

            "my friend's dog keeps chewing my shoes" (the dog is not the user's)
            life_facts: []

            "actually I don't have a dog" (explicit denial -> clear the fact)
            life_facts: [{{"key": "has_pet", "negated": true}}]

            Turn Scoring (apply when a previous assistant response is provided):
            The current user message is the "next-state signal" -- it reveals how well Buddy responded.
            Set signal_type to one of the following based on how the current message relates to the previous response:
            - re_query: user asks the same or very similar question again -> turn_score -1
            - correction: user says the answer was wrong, uses "I meant", "no actually",
              "that's not right", "you should have" -> turn_score -1
            - clarification: user asks what something means, "can you explain", "what do you mean" -> turn_score -1
            - acknowledgement: user builds on the answer without complaint, says "ok", "got it",
              "makes sense", continues the task naturally -> turn_score 1
            - praise: "perfect", "exactly", "thanks that's what I needed", "great" -> turn_score 1
            - none: no previous assistant response was provided -> turn_score 0

            Set directive_hint only when signal_type is "correction" or "re_query" AND the user message
            contains a concrete, actionable instruction about what Buddy should have done differently
            (e.g. "you should have checked the file first", "I wanted the short version not a list").
            Set to null for vague dissatisfaction without a clear directive.

            Return ONLY valid JSON. No explanation, no markdown fences.
            """


def _build_extraction_prompt(
    message: str,
    prev_user_query: str | None,
    prev_buddy_response: str | None,
) -> str:
    prev_query_block = (
        f"Previous user query (use only if current message is ambiguous): {prev_user_query}\n\n"
        if prev_user_query
        else ""
    )
    prev_response_block = (
        f"Previous assistant response (for turn scoring only): {prev_buddy_response[:500]}\n\n"
        if prev_buddy_response
        else ""
    )
    turn_scoring_note = (
        "Score the previous assistant response using the current message as the next-state signal. "
        "Populate turn_score, signal_type, and directive_hint per your instructions.\n\n"
        if prev_buddy_response
        else 'No previous assistant response. Set turn_score to 0, signal_type to "none", directive_hint to null.\n\n'
    )
    return (
        f"{prev_query_block}"
        f"{prev_response_block}"
        f"Current message: {message}\n\n"
        f"{turn_scoring_note}"
        "Extract insights as JSON:\n"
        "{\n"
        '  "primary_intent": "task_request|seeking_advice|information_lookup|casual_chat|venting|complaint|gratitude|follow_up_only",\n'
        '  "secondary_intent": "string or null",\n'
        '  "interests": [{"category": "one slug from the category list", "subject": "specific subject or null"}],\n'
        '  "life_facts": [{"key": "one key from the life fact list", "value": "concrete value or null", "negated": false}],\n'
        '  "domain": "work|health|finance|learning|social|entertainment|personal|technical|unclear",\n'
        '  "tone": "casual|terse|verbose|formal|playful",\n'
        '  "emotional_state": "neutral|anxious|frustrated|excited|anticipatory|curious|sad or null",\n'
        '  "urgency": "none|low|medium|high",\n'
        '  "response_depth_preference": "wants_brief|wants_detailed|wants_step_by_step|wants_examples|wants_opinion or null",\n'
        '  "question_type": "how_to|what_is|opinion_request|recommendation|comparison|troubleshooting or null",\n'
        '  "explicit_facts": ["durable identity/preference facts only -- no reminder times, dates, or task params"],\n'
        '  "inferred_goal_hints": ["high-confidence goals -- max 3"],\n'
        '  "used_prev_query_context": true or false,\n'
        '  "extraction_skipped": true or false,\n'
        '  "turn_score": 1 or -1 or 0,\n'
        '  "signal_type": "re_query|correction|clarification|acknowledgement|praise|none",\n'
        '  "directive_hint": "concise actionable instruction or null"\n'
        "}"
    )


def _sanitize_firestore_key(key: str) -> str:
    """
    Firestore field names cannot contain '.' or '/'.
    Keys are trimmed to 100 chars to stay well within Firestore limits.
    """
    return key.replace(".", "_").replace("/", "_").strip()[:100]


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


async def _write_user_aura_profile(uid: str, profile: dict[str, Any]) -> None:
    from .firebase import admin_firestore

    # Firestore hard-fails a document write above 1 MiB, and that failure is
    # swallowed downstream — which would silently freeze the profile. Warn loudly
    # while there is still headroom so a bloating doc never looks healthy.
    approx_bytes = len(json.dumps(profile, default=str).encode("utf-8"))
    if approx_bytes >= _PROFILE_SIZE_WARN_BYTES:
        logger.warn("UserAuraExtractor: profile approaching Firestore 1MB limit", {
            "user_id": uid,
            "approx_bytes": approx_bytes,
            "interest_categories": category_count(profile),
        })

    def _put() -> None:
        admin_firestore().collection("UserAura").document(uid).set(profile)

    await asyncio.to_thread(_put)


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


async def _user_has_granted_aura_consent(uid: str) -> bool:
    """Read aura_consent_granted from users/{uid}. Returns False on any error (safe default)."""
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


async def extract_and_update_user_aura(
    uid: str,
    message: str,
    session_id: str | None = None,
    prev_buddy_response: str | None = None,
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

    All exceptions are caught and logged. This function never raises.
    """
    # Step 0: GDPR consent gate. Skip if the user has not opted in, but log it so a
    # frozen profile (a user actively chatting whose Aura never updates) shows up in
    # logs instead of looking identical to "healthy and quiet". This skip being
    # silent is exactly what hid a 5-week profile freeze. The check reads
    # users/{uid}.aura_consent_granted, written at onboarding and by the memory toggle.
    if not await _user_has_granted_aura_consent(uid):
        logger.info("UserAuraExtractor: extraction skipped, Aura consent not granted", {
            "user_id": uid,
        })
        return

    insight: MessageInsight | None = None
    try:
        profile = await _read_user_aura_profile(uid)
        prev_query: str | None = profile.get("prev_user_query")

        prompt = _build_extraction_prompt(message, prev_query, prev_buddy_response)
        insight = cast(MessageInsight, await get_model_provider().cheap(
            prompt,
            system=_EXTRACTION_SYSTEM_PROMPT,
            response_model=MessageInsight,
            temperature=_EXTRACTION_TEMPERATURE,
        ))

        updated = _merge_profile(profile, insight, message)
        await _write_user_aura_profile(uid, updated)

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
        })

    except ValidationError as exc:
        logger.warn("UserAuraExtractor: insight parse failed -- Gemini returned malformed JSON", {
            "user_id": uid,
            "error": str(exc),
        })
    except Exception as exc:
        logger.warn("UserAuraExtractor: extraction failed", {
            "user_id": uid,
            "error": str(exc),
            "error_type": type(exc).__name__,
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
