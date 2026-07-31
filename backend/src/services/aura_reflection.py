"""
AuraReflection — the per-SESSION reflection tier of the UserAura profile.

Where the per-turn capture tier (`user_aura_extractor`) sees ONE message and appends
flat, fast signals, the reflection tier sees the WHOLE session transcript once it ends
and writes the narrative layer a single message can't:

  * storylines  — what is GOING ON ("wrote a tensor-parallelism blog to land an SDE
                  role at Annapurna Labs"), fused across consecutive messages, with an
                  intent and a kind (durable / event_driven / goal_instrumental).
  * traits      — personality signals ("passion-oriented"), stored on every inference
                  but only SHOWN once corroborated (the gate lives in the schema).
  * interest-kind merge-ops — reclassify a flat interest the capture tier wrote, e.g.
                  mark "FIFA World Cup" event_driven so a one-off "send me updates" ask
                  fades in a week instead of looking like a standing football obsession.
  * session_summary — a short prose digest for the chat / voice prompt.

Design notes (see plan + CLAUDE.md two-tier architecture):
  - Runs on the BALANCED tier (Claude Haiku), not cheap Flash: it is one call per
    SESSION (not per message), so a stronger reasoning model is affordable, and the
    narrative/trait inference is the whole point.
  - The LLM call happens OUTSIDE any lock. The resulting patch is applied inside a SHORT
    Firestore transaction that re-reads the current profile and folds in only the
    reflection-owned fields (interests touched only as merge-ops on the current map),
    so a concurrent capture write can never be lost.
  - Idempotent per session_id (a ring on the doc) so a retried / double-sent
    consolidation is a no-op.
  - GDPR-gated on `aura_consent_granted`, exactly like the capture tier.
  - Every failure is swallowed and logged. Reflection must never raise into a request
    or a scheduler tick.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from google.cloud import firestore as fs
from pydantic import BaseModel, field_validator

from ..lib.logger import logger
from ..prompts import AURA_REFLECTION_SYSTEM_PROMPT, aura_reflection_user_prompt
from .life_facts_schema import (
    LIFE_FACT_KEYS,
    LIFE_FACTS_FIELD,
    apply_life_fact,
    remove_life_fact,
)
from .model_provider import get_model_provider
from .user_aura_extractor import _user_has_granted_aura_consent
from .user_aura_schema import (
    DEFAULT_INTEREST_KIND,
    INTEREST_KINDS,
    apply_storyline,
    apply_trait,
    prune_interest,
    reclassify_interest_kind,
)

# A session needs at least this many USER turns to have an arc worth reflecting on.
# One-line sessions ("what's 5km in miles") produce no narrative.
MIN_USER_TURNS = 2
# Above this many turns, compress the transcript via the cheap tier (map) before the
# single balanced reflection call (reduce), so a marathon session stays cheap and never
# blows the prompt / 1 MiB budgets.
MAP_REDUCE_WINDOW_TURNS = 40
# Caps on what one reflection may write, so a hallucinating model can't flood the doc.
MAX_PATCH_STORYLINES = 5
MAX_PATCH_TRAITS = 5
MAX_PATCH_INTEREST_OPS = 10
MAX_PATCH_PRUNE = 10
MAX_PATCH_LIFE_FACT_FIXES = 5
# Caps on the compacted capture lists the reflection rewrites (match the capture tier).
MAX_CANONICAL_FACTS = 20
MAX_CANONICAL_GOALS = 10
# Idempotency: remember the last-reflected TURN COUNT per session, so a GROWN session
# re-reflects (the old id-only ring froze a session after its first reflection, which is
# why a long multi-topic session only ever produced one storyline). Capped.
REFLECTED_SESSIONS_CAP = 50
# Defensive cap on transcript characters fed to the model after compression.
MAX_TRANSCRIPT_CHARS = 16_000


# --------------------------------------------------------------------------
# Patch model — the structured output of one reflection call
# --------------------------------------------------------------------------

class ReflectionStoryline(BaseModel):
    """One ongoing thread in the user's life, fused from the whole session."""

    id: str                       # stable slug; REUSE an existing id to continue a storyline
    summary: str
    entities: list[str] = []
    categories: list[str] = []
    intent: str | None = None
    kind: str = DEFAULT_INTEREST_KIND  # durable | event_driven | goal_instrumental
    confidence: float = 0.5

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value: object) -> str:
        v = str(value or "").strip().lower()
        return v if v in INTEREST_KINDS else DEFAULT_INTEREST_KIND


class ReflectionTrait(BaseModel):
    """A personality signal. Stored on every inference; only SHOWN once corroborated."""

    name: str
    confidence: float = 0.5


class InterestKindOp(BaseModel):
    """Reclassify a flat interest the capture tier wrote (e.g. FIFA -> event_driven)."""

    category: str
    subject: str
    kind: str

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, value: object) -> str:
        v = str(value or "").strip().lower()
        return v if v in INTEREST_KINDS else DEFAULT_INTEREST_KIND


class InterestPruneOp(BaseModel):
    """Remove a mis-attributed flat interest (e.g. a gift for someone else that the fast
    layer stored as the user's own interest)."""

    category: str
    subject: str


class LifeFactCorrection(BaseModel):
    """Fix or clear a wrong life fact. value=None clears it (e.g. a relocation destination
    wrongly stored as home_city)."""

    key: str
    value: str | None = None


class ReflectionPatch(BaseModel):
    session_summary: str = ""
    storylines: list[ReflectionStoryline] = []
    traits: list[ReflectionTrait] = []
    interest_kind_ops: list[InterestKindOp] = []
    interest_prune: list[InterestPruneOp] = []
    life_fact_corrections: list[LifeFactCorrection] = []
    # The CLEAN, de-duplicated rewrite of the capture-tier lists, merged with anything new
    # from this session. Empty = leave the existing list as-is (anti-wipe).
    facts_canonical: list[str] = []
    goals_canonical: list[str] = []


def _normalize_turns(turns: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], int]:
    """Return [(role, text)] keeping only non-empty user/assistant turns, plus the user-turn count."""
    cleaned: list[tuple[str, str]] = []
    user_turns = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role", "")).strip().lower()
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        if role in ("user", "human"):
            cleaned.append(("User", text))
            user_turns += 1
        elif role in ("assistant", "buddy", "model"):
            cleaned.append(("Buddy", text))
    return cleaned, user_turns


def _render_transcript(turns: list[tuple[str, str]]) -> str:
    return "\n".join(f"{role}: {text}" for role, text in turns)


async def _compress_if_long(turns: list[tuple[str, str]]) -> str:
    """Map-reduce for long sessions: summarize each window with the CHEAP tier, then the
    reflection call (reduce) runs on the summaries. Short sessions pass through verbatim."""
    if len(turns) <= MAP_REDUCE_WINDOW_TURNS:
        return _render_transcript(turns)[:MAX_TRANSCRIPT_CHARS]

    provider = get_model_provider()
    chunks = [
        turns[i:i + MAP_REDUCE_WINDOW_TURNS]
        for i in range(0, len(turns), MAP_REDUCE_WINDOW_TURNS)
    ]
    summaries: list[str] = []
    for chunk in chunks:
        try:
            summary = await provider.cheap(
                "Summarize this conversation segment in 3-5 dense sentences, keeping every "
                "concrete person, place, org, product, goal, and the user's intent:\n\n"
                + _render_transcript(chunk),
                temperature=0.2,
            )
            summaries.append(str(summary).strip())
        except Exception as exc:  # one bad chunk must not sink the whole reflection
            logger.warn("AuraReflection: chunk summarize failed, skipping chunk", {"error": str(exc)})
    joined = "\n\n".join(s for s in summaries if s)
    return joined[:MAX_TRANSCRIPT_CHARS]


def _profile_context_block(profile: dict[str, Any]) -> str:
    """Compact snapshot of the CURRENT profile so the model can: reuse a storyline id,
    de-duplicate the fact/goal lists against what's already stored, reclassify/prune the
    real interest subjects the fast layer wrote, and correct wrong life facts. Kept small."""
    lines: list[str] = []

    storylines = profile.get("storylines")
    if isinstance(storylines, dict) and storylines:
        sl = [
            f"  - {sid}: {node['summary']}"
            for sid, node in list(storylines.items())[:8]
            if isinstance(node, dict) and node.get("summary")
        ]
        if sl:
            lines.append("Existing storylines (reuse an id to continue one):\n" + "\n".join(sl))

    facts = profile.get("explicit_facts")
    if isinstance(facts, list) and facts:
        lines.append(
            "Current durable facts:\n"
            + "\n".join(f"  - {f}" for f in facts[:30] if isinstance(f, str))
        )

    goals = profile.get("inferred_goals")
    if isinstance(goals, list) and goals:
        lines.append(
            "Current goals:\n"
            + "\n".join(f"  - {g}" for g in goals[:20] if isinstance(g, str))
        )

    life_facts = profile.get(LIFE_FACTS_FIELD)
    if isinstance(life_facts, dict) and life_facts:
        lf = [
            f"{k}={v.get('value')}"
            for k, v in life_facts.items()
            if isinstance(v, dict) and v.get("value")
        ]
        if lf:
            lines.append("Current life facts: " + ", ".join(lf))

    interests = profile.get("interests")
    if isinstance(interests, dict) and interests:
        il: list[str] = []
        for cat, node in list(interests.items())[:12]:
            if not isinstance(node, dict):
                continue
            subs = node.get("subjects")
            if isinstance(subs, dict) and subs:
                names = [
                    str(s.get("display", k))
                    for k, s in subs.items()
                    if isinstance(s, dict)
                ][:6]
                if names:
                    il.append(f"  - {cat}: {', '.join(names)}")
        if il:
            lines.append("Current interests (subjects the fast layer stored):\n" + "\n".join(il))

    return "\n\n".join(lines) if lines else "Existing profile: (empty)"


async def _read_profile(uid: str) -> dict[str, Any]:
    from .firebase import admin_firestore

    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(uid).get()
        return snap.to_dict() or {}

    return await asyncio.to_thread(_fetch)


def _session_already_reflected(
    profile: dict[str, Any], session_id: str | None, turn_count: int
) -> bool:
    """True only when this session was ALREADY reflected at >= this turn count, so a GROWN
    session (more turns than last time) is allowed through. A session frozen by the legacy
    id-only ring is treated as not-yet-reflected so it backfills once."""
    if not session_id:
        return False
    reflected = profile.get("reflected_sessions")
    if isinstance(reflected, dict):
        prior = reflected.get(session_id)
        if isinstance(prior, (int, float)) and int(prior) > 0 and int(prior) >= turn_count:
            return True
    return False


def _replace_list_if_present(
    profile: dict[str, Any], field: str, canonical: list[str], cap: int
) -> None:
    """Replace a capture-tier list (explicit_facts / inferred_goals) with the reflection's
    cleaned, de-duplicated version. Anti-wipe: an empty canonical NEVER erases an existing
    list (a model error must not wipe the profile); exact repeats dropped, order preserved."""
    if not isinstance(canonical, list):
        return
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in canonical:
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    if not cleaned:
        return
    profile[field] = cleaned[:cap]


def _cap_reflected_sessions(reflected: dict[str, Any]) -> dict[str, Any]:
    if len(reflected) <= REFLECTED_SESSIONS_CAP:
        return reflected
    # dict preserves insertion order; the writer re-inserts on each touch, so the tail is
    # the most-recently reflected. Keep the tail.
    return dict(list(reflected.items())[-REFLECTED_SESSIONS_CAP:])


def _fold_patch_into_profile(
    profile: dict[str, Any],
    session_id: str | None,
    turn_count: int,
    patch: ReflectionPatch,
    now: datetime,
) -> bool:
    """Fold a reflection patch into a profile dict IN PLACE (pure, no I/O).

    Adds the narrative layer (storylines/traits) AND cleans the capture-tier lists (dedupe
    facts/goals, prune mis-attributed interests, reclassify event-driven ones, correct wrong
    life facts). interests are touched only on the map already present. Returns False if this
    session was already reflected at >= this turn count (idempotent, but a grown session
    passes). Kept pure so the transaction wrapper stays thin and the logic is unit-testable
    without Firestore."""
    if _session_already_reflected(profile, session_id, turn_count):
        return False

    # Narrative: storylines (merge by id) + traits (corroborated by DISTINCT session).
    storylines = profile.get("storylines")
    if not isinstance(storylines, dict):
        storylines = {}
    for s in patch.storylines[:MAX_PATCH_STORYLINES]:
        apply_storyline(
            storylines, s.id, s.summary, s.entities, s.categories,
            s.intent, s.kind, s.confidence, now,
        )
    profile["storylines"] = storylines

    traits = profile.get("traits")
    if not isinstance(traits, dict):
        traits = {}
    for t in patch.traits[:MAX_PATCH_TRAITS]:
        apply_trait(traits, t.name, t.confidence, now, session_id=session_id)
    profile["traits"] = traits

    # Interests: reclassify kind (e.g. FIFA -> event_driven) then prune mis-attributions.
    interests = profile.get("interests")
    if isinstance(interests, dict):
        for op in patch.interest_kind_ops[:MAX_PATCH_INTEREST_OPS]:
            reclassify_interest_kind(interests, op.category, op.subject, op.kind)
        for op in patch.interest_prune[:MAX_PATCH_PRUNE]:
            prune_interest(interests, op.category, op.subject)
        profile["interests"] = interests

    # Life-fact corrections (clear a destination wrongly stored as home, etc.).
    if patch.life_fact_corrections:
        life_facts = profile.get(LIFE_FACTS_FIELD)
        if not isinstance(life_facts, dict):
            life_facts = {}
        for fix in patch.life_fact_corrections[:MAX_PATCH_LIFE_FACT_FIXES]:
            if fix.key not in LIFE_FACT_KEYS:
                continue
            if fix.value is None or not str(fix.value).strip():
                remove_life_fact(life_facts, fix.key)
            else:
                apply_life_fact(life_facts, fix.key, str(fix.value).strip(), now)
        profile[LIFE_FACTS_FIELD] = life_facts

    # Compact the noisy capture lists into the reflection's clean, de-duplicated view.
    _replace_list_if_present(profile, "explicit_facts", patch.facts_canonical, MAX_CANONICAL_FACTS)
    _replace_list_if_present(profile, "inferred_goals", patch.goals_canonical, MAX_CANONICAL_GOALS)

    if patch.session_summary.strip():
        profile["session_summary"] = patch.session_summary.strip()

    if session_id:
        reflected = profile.get("reflected_sessions")
        if not isinstance(reflected, dict):
            reflected = {}
        reflected.pop(session_id, None)         # re-insert at the tail (recency order)
        reflected[session_id] = turn_count
        profile["reflected_sessions"] = _cap_reflected_sessions(reflected)
        profile.pop("consolidated_session_ids", None)  # migrate off the legacy field
    profile["last_reflection_at"] = now.isoformat()
    return True


def _apply_patch_txn(
    uid: str, session_id: str | None, turn_count: int, patch: ReflectionPatch, now: datetime
) -> bool:
    """Apply the reflection patch to UserAura/{uid} inside a short transaction.

    Re-reads the CURRENT profile inside the transaction and folds the patch in, so a capture
    write that landed since the LLM call is never lost; the transaction simply retries on
    contention. Returns False if the session was already reflected at this size."""
    from .firebase import admin_firestore

    db = admin_firestore()
    ref = db.collection("UserAura").document(uid)
    transaction = db.transaction()

    @fs.transactional
    def _apply(txn: fs.Transaction) -> bool:
        snap = ref.get(transaction=txn)
        profile = snap.to_dict() or {}
        applied = _fold_patch_into_profile(profile, session_id, turn_count, patch, now)
        if applied:
            txn.set(ref, profile)
        return applied

    return _apply(transaction)


async def reflect_session(
    turns: list[dict[str, Any]],
    existing_profile: dict[str, Any] | None = None,
) -> ReflectionPatch | None:
    """Run ONLY the LLM reflection over a session's turns and return the structured
    patch. No consent check, no Firestore, no idempotency — pure model I/O. Returns None
    when the session is too small to reflect on. Exposed so the golden eval can exercise
    the reflection prompt directly, and so consolidate_session has one place that calls
    the model."""
    cleaned, user_turns = _normalize_turns(turns)
    if user_turns < MIN_USER_TURNS:
        return None
    profile = existing_profile or {}
    transcript = await _compress_if_long(cleaned)
    prompt = aura_reflection_user_prompt(
        profile_context=_profile_context_block(profile),
        transcript=transcript,
    )
    return cast(ReflectionPatch, await get_model_provider().balanced(
        prompt,
        system=AURA_REFLECTION_SYSTEM_PROMPT,
        response_model=ReflectionPatch,
        temperature=0.3,
    ))


async def _upsert_graph_from_patch(uid: str, patch: ReflectionPatch) -> None:
    """Build the Phase 1 graph from reflection output without another model call."""
    try:
        from .memory.atom_store import AtomInput
        from .memory.fields import ATOM_TYPE_FACT, ATOM_TYPE_STORYLINE
        from .memory.graph_store import (
            GraphEdgeInput,
            atom_node,
            entity_node,
            upsert_graph,
        )

        nodes = []
        edges = []
        for storyline in patch.storylines or []:
            summary = (storyline.summary or "").strip()
            if not summary:
                continue
            project = entity_node(storyline.id or summary)
            story_atom = atom_node(
                ATOM_TYPE_STORYLINE,
                summary,
                project_id=project.node_id,
                weight=storyline.confidence,
            )
            nodes.extend((project, story_atom))
            edges.append(GraphEdgeInput(story_atom.node_id, project.node_id, "about"))
            for raw_entity in storyline.entities or []:
                if not isinstance(raw_entity, str) or not raw_entity.strip():
                    continue
                entity = entity_node(raw_entity, project_id=project.node_id)
                nodes.append(entity)
                edges.append(GraphEdgeInput(story_atom.node_id, entity.node_id, "mentions"))
            for category in storyline.categories or []:
                if not isinstance(category, str) or not category.strip():
                    continue
                category_node = entity_node(category, project_id=project.node_id)
                nodes.append(category_node)
                edges.append(GraphEdgeInput(
                    story_atom.node_id, category_node.node_id, "categorized_as",
                ))
        for fact in patch.facts_canonical or []:
            if isinstance(fact, str) and fact.strip():
                nodes.append(atom_node(ATOM_TYPE_FACT, fact.strip(), weight=0.6))

        if nodes or edges:
            await upsert_graph(uid, nodes, edges, source="reflection")
    except Exception as exc:
        logger.warn("AuraReflection: graph build failed", {
            "user_id": uid,
            "error": str(exc),
        })


async def _upsert_memory_atoms_from_patch(uid: str, patch: ReflectionPatch) -> None:
    """Mirror reflected storylines + canonical facts into the UNBOUNDED long-term memory
    store (services/memory) for query-relevant semantic recall. Traits are intentionally
    NOT stored as atoms: they are identity labels already surfaced via the digest's
    shown_traits, not episodic things to recall against a query. Fire-and-forget;
    upsert_atoms swallows its own errors."""
    from .memory.atom_store import AtomInput, upsert_atoms
    from .memory.fields import ATOM_TYPE_FACT, ATOM_TYPE_STORYLINE

    atoms: list[AtomInput] = []
    for storyline in patch.storylines or []:
        summary = (storyline.summary or "").strip()
        if summary:
            atoms.append(AtomInput(
                text=summary,
                atom_type=ATOM_TYPE_STORYLINE,
                decay_kind=storyline.kind,
                importance=storyline.confidence,
                categories=list(storyline.categories or []),
            ))
    for fact in patch.facts_canonical or []:
        if isinstance(fact, str) and fact.strip():
            atoms.append(AtomInput(text=fact.strip(), atom_type=ATOM_TYPE_FACT, importance=0.6))
    if atoms:
        await upsert_atoms(uid, atoms, source="reflection")
    await _upsert_graph_from_patch(uid, patch)


async def consolidate_session(
    uid: str,
    session_id: str | None,
    turns: list[dict[str, Any]],
    modality: str = "text",
) -> None:
    """Reflect over one finished session and fold the narrative into UserAura/{uid}.

    Safe to call fire-and-forget. Never raises: every failure is logged and swallowed.
    The shared entry point for BOTH text chat and voice (voice passes its transcript here),
    so there is exactly one session-reflection implementation.
    """
    try:
        # GDPR gate — identical to the capture tier. No consent, no profiling.
        if not await _user_has_granted_aura_consent(uid):
            logger.info("AuraReflection: skipped, Aura consent not granted", {"user_id": uid})
            return

        cleaned, user_turns = _normalize_turns(turns)
        if user_turns < MIN_USER_TURNS:
            logger.info("AuraReflection: skipped, session too small to reflect on", {
                "user_id": uid, "user_turns": user_turns,
            })
            return
        turn_count = len(cleaned)

        # Idempotency pre-check (avoids an LLM call on an already-reflected session) — but a
        # GROWN session (more turns than last time) passes through and re-reflects.
        profile = await _read_profile(uid)
        if _session_already_reflected(profile, session_id, turn_count):
            logger.info("AuraReflection: skipped, session already reflected at this size", {
                "user_id": uid, "session_id": session_id, "turn_count": turn_count,
            })
            return

        patch = await reflect_session(turns, profile)
        if patch is None:
            return

        now = datetime.now(UTC)
        applied = await asyncio.to_thread(_apply_patch_txn, uid, session_id, turn_count, patch, now)

        # Mirror reflected storylines + canonical facts into the unbounded memory store.
        await _upsert_memory_atoms_from_patch(uid, patch)

        logger.info("AuraReflection: session consolidated", {
            "user_id": uid,
            "session_id": session_id,
            "modality": modality,
            "applied": applied,
            "turn_count": turn_count,
            "storylines": len(patch.storylines),
            "traits": len(patch.traits),
            "interest_kind_ops": len(patch.interest_kind_ops),
            "interest_prune": len(patch.interest_prune),
            "facts_canonical": len(patch.facts_canonical),
            "goals_canonical": len(patch.goals_canonical),
            "life_fact_corrections": len(patch.life_fact_corrections),
            "user_turns": user_turns,
        })
    except Exception as exc:
        logger.warn("AuraReflection: consolidation failed", {
            "user_id": uid,
            "session_id": session_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
