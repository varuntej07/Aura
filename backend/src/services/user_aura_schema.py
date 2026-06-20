"""
UserAura interest schema — the single source of truth for the behavioral
interest taxonomy and the read/write helpers around it.

Both sides of the field-name contract live here so a rename can never silently
flatten the signal (per the data-layer discipline in CLAUDE.md):

  * The WRITER (`user_aura_extractor._merge_profile`) calls `apply_interest_signal`
    to fold one extracted (category, subject) pair into the profile.
  * Every READER (chat prompt, voice prompt, notification framer, signal-engine
    user_vector) calls the ranked accessors here instead of reaching into the
    raw Firestore maps.

Stored shape on `UserAura/{uid}`:

    "interests": {
        "<category_slug>": {
            "weight": float,            # time-decayed score (see HALF_LIFE_BY_KIND)
            "first_seen": iso8601,
            "last_seen":  iso8601,
            "subjects": {
                "<sanitized_key>": {
                    "display": str,     # original casing, e.g. "KCR"
                    "weight": float,    # time-decayed score (see HALF_LIFE_BY_KIND)
                    "first_seen": iso8601,
                    "last_seen":  iso8601,
                }
            }
        }
    }

Recency is baked into `weight`: on every hit the stored weight is first decayed
to "now", then incremented. Rankers decay again to the read time, so an interest
the user dropped fades on its own without any sweep job.

Legacy fallback: the previous design stored a flat `deep_interest_frequencies`
map ({free_text: count}). While existing profiles rebuild into the new structure,
the prompt/embedding accessors supplement from that legacy map so chat, voice and
notifications never go blank during the transition.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Closed taxonomy. Broad, life-spanning, culture-agnostic. The model maps each
# query to exactly one of these; anything it cannot place lands in OTHER_CATEGORY.
# Adding a category later = append one slug here + one label below (and, if the
# signal-engine ever embeds category labels directly, nothing else changes).
OTHER_CATEGORY = "other"

CATEGORY_LABELS: dict[str, str] = {
    "politics_governance": "politics & governance",
    "regional_local_affairs": "regional & local affairs",
    "news_current_affairs": "news & current affairs",
    "business_economy": "business & economy",
    "personal_finance": "personal finance",
    "real_estate_property": "real estate & property",
    "health_medical": "health & medical",
    "fitness_nutrition": "fitness & nutrition",
    "food_cooking": "food & cooking",
    "technology_computing": "technology & computing",
    "science_nature": "science & nature",
    "education_learning": "education & learning",
    "language_translation": "language & translation",
    "career_jobs": "career & jobs",
    "travel_geography": "travel & geography",
    "automotive": "automotive",
    "sports": "sports",
    "entertainment_media": "entertainment & media",
    "arts_history_culture": "arts, history & culture",
    "religion_spirituality": "religion & spirituality",
    "relationships_social": "relationships & social",
    "family_parenting": "family & parenting",
    "home_shopping_lifestyle": "home, shopping & lifestyle",
    "fashion_beauty": "fashion & beauty",
    "gaming": "gaming",
    "law_legal": "law & legal",
    "astrology_beliefs": "astrology & beliefs",
    "self_improvement": "self-improvement",
    "general_knowledge": "general knowledge",
    OTHER_CATEGORY: "other",
}

# Tuple of valid slugs — used by the writer to coerce unknown LLM output to OTHER.
INTEREST_CATEGORIES: tuple[str, ...] = tuple(CATEGORY_LABELS.keys())

# Provenance of an interest node. An interest can be explicitly declared at
# onboarding, passively learned from chat, or both. Stored on each interest node
# so a reader can tell a durable declared interest from a decaying learned one.
INTEREST_ORIGIN_ONBOARDING = "onboarding"
INTEREST_ORIGIN_LEARNED = "learned"
INTEREST_ORIGIN_BOTH = "both"

# Field names written on the users/{uid} doc at onboarding. Defined here (the one
# taxonomy module both onboarding writer and signal-engine reader import) so the
# field-name contract lives in exactly one place per the data-layer discipline.
ONBOARDING_INTERESTS_FIELD = "onboarding_interests"  # list[taxonomy slug]
GENDER_FIELD = "gender"
LOCALE_FIELD = "locale"
LANGUAGE_FIELD = "language"

# Interest / storyline KIND drives how fast a node fades. event_driven follows a
# transient happening ("send me World Cup updates" while the Cup is on) and must
# fade fast so a one-off ask never looks like a standing passion; durable is a real
# standing interest; goal_instrumental is pursued in service of an active goal (a
# blog written to land a specific job) and sits between the two.
INTEREST_KIND_DURABLE = "durable"
INTEREST_KIND_EVENT_DRIVEN = "event_driven"
INTEREST_KIND_GOAL_INSTRUMENTAL = "goal_instrumental"
INTEREST_KINDS: tuple[str, ...] = (
    INTEREST_KIND_DURABLE,
    INTEREST_KIND_EVENT_DRIVEN,
    INTEREST_KIND_GOAL_INSTRUMENTAL,
)
# A node with no kind (every node the capture tier writes, and every pre-existing
# prod document) is treated as durable, so the change is backward-safe: legacy data
# keeps a sensible slow decay instead of suddenly vanishing.
DEFAULT_INTEREST_KIND = INTEREST_KIND_DURABLE
# Recency half-life per kind (days): a node's weight halves every this-many days of
# inactivity. event_driven fades in a week; durable persists for a quarter.
HALF_LIFE_BY_KIND: dict[str, float] = {
    INTEREST_KIND_DURABLE: 90.0,
    INTEREST_KIND_EVENT_DRIVEN: 7.0,
    INTEREST_KIND_GOAL_INSTRUMENTAL: 45.0,
}
# Per-category cap on distinct subjects; lowest-weight subjects are evicted.
MAX_SUBJECTS_PER_CATEGORY = 15
# Max subject string length we will store (defence against runaway LLM output).
MAX_SUBJECT_LENGTH = 80
# Caps for the reflection-tier structures (keep the doc well under Firestore's 1 MiB).
MAX_STORYLINES = 12
MAX_TRAITS = 20
# A trait is SHOWN only once corroborated: at least this much evidence AND confidence.
# One eager or flattering inference can never become a visible permanent label, since
# the user reads their own Aura. Promotion is a read-time decision, never stored, so
# the thresholds can change without a migration.
TRAIT_MIN_EVIDENCE = 2
TRAIT_MIN_CONFIDENCE = 0.7
# Cap on the distinct-session ids stored per trait (corroboration evidence). Plenty for
# the >=2 gate while bounding doc growth.
MAX_TRAIT_SESSIONS = 10

# Legacy flat map produced by the pre-taxonomy extractor. Read-only fallback.
# Intentionally RETAINED on the doc (frozen, no longer written): the shipped
# Flutter app's Aura profile screen still reads it, and the backend accessors
# fall back to it for not-yet-rebuilt profiles. Remove it from the doc only after
# the app update that reads `interests` has rolled out to every client.
LEGACY_DEEP_INTEREST_FIELD = "deep_interest_frequencies"
# Once the new structure holds at least this many categories, the dead maps are
# considered stale and can be dropped by the writer to reclaim space.
LEGACY_SUNSET_CATEGORY_COUNT = 5
# Maps the previous design wrote but NOTHING anywhere ever read (backend or app) —
# always safe to drop once the new structure is mature. deep_interest_frequencies
# is deliberately NOT here; see the note above.
DEAD_INTEREST_FIELDS = (
    "surface_topic_frequencies",
    "named_entities_seen",
)


def category_label(slug: str) -> str:
    """Human-readable label for a category slug. Falls back to the slug itself."""
    return CATEGORY_LABELS.get(slug, slug.replace("_", " "))


def sanitize_firestore_key(key: str) -> str:
    """
    Firestore map keys cannot contain '.' or '/'. Trim to 100 chars to stay well
    within limits. Shared by the writer for both interest subjects and the other
    frequency maps so the rule lives in one place.
    """
    return key.replace(".", "_").replace("/", "_").strip()[:100]


# --------------------------------------------------------------------------
# Decay math
# --------------------------------------------------------------------------

def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _half_life_for_kind(kind: Any) -> float:
    """Half-life (days) for a node's kind. Unknown/missing kind falls back to durable."""
    if isinstance(kind, str) and kind in HALF_LIFE_BY_KIND:
        return HALF_LIFE_BY_KIND[kind]
    return HALF_LIFE_BY_KIND[DEFAULT_INTEREST_KIND]


def decayed_weight_by_kind(weight: Any, last_seen: Any, kind: Any, now: datetime) -> float:
    """Stored weight decayed forward to `now` using the half-life for `kind`.

    A missing or unknown kind decays as durable (the slow lane), so capture-written
    nodes (which carry no kind) and every legacy document keep a sensible decay. Only
    nodes the reflection tier has explicitly marked event_driven fade fast.

    Returns 0.0 for unusable input.
    """
    try:
        base = float(weight)
    except (TypeError, ValueError):
        return 0.0
    if base <= 0:
        return 0.0
    last = _parse_iso(last_seen)
    if last is None:
        return base
    days = max(0.0, (now - last).total_seconds() / 86400.0)
    return base * (0.5 ** (days / _half_life_for_kind(kind)))


# --------------------------------------------------------------------------
# Writer primitive
# --------------------------------------------------------------------------

def _merge_origin(existing: Any, incoming: str) -> str:
    """Combine a node's existing origin with a new one. onboarding + learned = both."""
    if existing not in (
        INTEREST_ORIGIN_ONBOARDING, INTEREST_ORIGIN_LEARNED, INTEREST_ORIGIN_BOTH,
    ):
        return incoming
    if existing == INTEREST_ORIGIN_BOTH or existing == incoming:
        return existing
    return INTEREST_ORIGIN_BOTH


def apply_interest_signal(
    interests: dict[str, Any],
    category: str,
    subject: str | None,
    now: datetime,
    origin: str = INTEREST_ORIGIN_LEARNED,
) -> None:
    """
    Fold one (category, subject) signal into the `interests` map in place.

    Unknown categories are coerced to OTHER_CATEGORY. The category weight always
    advances; the subject weight advances only when a concrete subject is given.
    Both use decay-then-increment so the stored number is recency-aware.

    `origin` records provenance: passive chat signals default to "learned"; the
    onboarding seeder passes "onboarding". A node touched by both becomes "both".
    """
    slug = category if category in INTEREST_CATEGORIES else OTHER_CATEGORY
    now_iso = now.isoformat()

    node = interests.get(slug)
    if not isinstance(node, dict):
        node = {
            "weight": 0.0, "first_seen": now_iso, "last_seen": now_iso,
            "subjects": {}, "origin": origin,
        }
        interests[slug] = node
    node["weight"] = decayed_weight_by_kind(node.get("weight"), node.get("last_seen"), node.get("kind"), now) + 1.0
    node["last_seen"] = now_iso
    node["origin"] = _merge_origin(node.get("origin"), origin)
    node.setdefault("first_seen", now_iso)

    subjects = node.setdefault("subjects", {})
    if not isinstance(subjects, dict):
        subjects = {}
        node["subjects"] = subjects

    clean = (subject or "").strip()
    if not clean or len(clean) > MAX_SUBJECT_LENGTH:
        return
    key = sanitize_firestore_key(clean)
    if not key:
        return

    snode = subjects.get(key)
    if not isinstance(snode, dict):
        snode = {"display": clean, "weight": 0.0, "first_seen": now_iso, "last_seen": now_iso}
        subjects[key] = snode
    snode["weight"] = decayed_weight_by_kind(snode.get("weight"), snode.get("last_seen"), snode.get("kind"), now) + 1.0
    snode["last_seen"] = now_iso
    snode["display"] = clean
    snode.setdefault("first_seen", now_iso)

    _cap_subjects(subjects, now)


def _cap_subjects(subjects: dict[str, Any], now: datetime) -> None:
    if len(subjects) <= MAX_SUBJECTS_PER_CATEGORY:
        return
    ranked = sorted(
        subjects.items(),
        key=lambda kv: decayed_weight_by_kind(kv[1].get("weight"), kv[1].get("last_seen"), kv[1].get("kind"), now),
        reverse=True,
    )
    for stale_key, _ in ranked[MAX_SUBJECTS_PER_CATEGORY:]:
        subjects.pop(stale_key, None)


# --------------------------------------------------------------------------
# Ranked readers (new structure)
# --------------------------------------------------------------------------

def _ranked_categories(profile: dict[str, Any], now: datetime) -> list[tuple[str, float, dict[str, Any]]]:
    interests = profile.get("interests")
    if not isinstance(interests, dict):
        return []
    rows: list[tuple[str, float, dict[str, Any]]] = []
    for slug, node in interests.items():
        if not isinstance(node, dict):
            continue
        weight = decayed_weight_by_kind(node.get("weight"), node.get("last_seen"), node.get("kind"), now)
        if weight > 0:
            rows.append((str(slug), weight, node))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def _ranked_subject_displays(node: dict[str, Any], now: datetime, k: int) -> list[str]:
    subjects = node.get("subjects")
    if not isinstance(subjects, dict) or not subjects:
        return []
    ranked = sorted(
        subjects.values(),
        key=lambda s: decayed_weight_by_kind(s.get("weight"), s.get("last_seen"), s.get("kind"), now) if isinstance(s, dict) else 0.0,
        reverse=True,
    )
    out: list[str] = []
    for s in ranked:
        if isinstance(s, dict) and s.get("display"):
            out.append(str(s["display"]))
        if len(out) >= k:
            break
    return out


def _legacy_top_keys(profile: dict[str, Any], k: int) -> list[str]:
    raw = profile.get(LEGACY_DEEP_INTEREST_FIELD)
    if not isinstance(raw, dict) or not raw:
        return []
    try:
        ranked = sorted(raw.items(), key=lambda kv: int(kv[1]), reverse=True)
    except (TypeError, ValueError):
        return []
    return [str(key) for key, _ in ranked[:k]]


# --------------------------------------------------------------------------
# Consumer-facing accessors
# --------------------------------------------------------------------------

def has_interest_data(profile: dict[str, Any], now: datetime | None = None) -> bool:
    """True if there is any new-structure or legacy interest signal to inject."""
    now = now or datetime.now(UTC)
    return bool(_ranked_categories(profile, now)) or bool(_legacy_top_keys(profile, 1))


def interest_prompt_lines(
    profile: dict[str, Any],
    now: datetime | None = None,
    k_categories: int = 3,
    k_subjects: int = 3,
) -> list[str]:
    """
    Lines for the chat / voice system prompt, e.g.
    "politics & governance: KCR, Telangana DGP". Falls back to legacy free-text
    keys when the new structure has fewer than k_categories entries.
    """
    now = now or datetime.now(UTC)
    lines: list[str] = []
    seen_labels: set[str] = set()
    for slug, _, node in _ranked_categories(profile, now)[:k_categories]:
        label = category_label(slug)
        seen_labels.add(label)
        subjects = _ranked_subject_displays(node, now, k_subjects)
        lines.append(f"{label}: {', '.join(subjects)}" if subjects else label)

    if len(lines) < k_categories:
        for key in _legacy_top_keys(profile, k_categories - len(lines)):
            if key not in seen_labels:
                lines.append(key)
                seen_labels.add(key)
    return lines


def top_interest_subjects(
    profile: dict[str, Any],
    now: datetime | None = None,
    k: int = 3,
) -> list[str]:
    """
    The most relevant specific subjects across all categories (e.g.
    ["KCR", "XUV 3XO"]). Used by the notification framer for the personalization
    edge. Supplemented with legacy keys when the new structure is sparse.
    """
    now = now or datetime.now(UTC)
    scored: list[tuple[str, float]] = []
    interests = profile.get("interests")
    if isinstance(interests, dict):
        for node in interests.values():
            if not isinstance(node, dict):
                continue
            subjects = node.get("subjects")
            if not isinstance(subjects, dict):
                continue
            for s in subjects.values():
                if isinstance(s, dict) and s.get("display"):
                    scored.append((str(s["display"]), decayed_weight_by_kind(s.get("weight"), s.get("last_seen"), s.get("kind"), now)))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    out = [name for name, _ in scored[:k]]

    if len(out) < k:
        for key in _legacy_top_keys(profile, k - len(out)):
            if key not in out:
                out.append(key)
    return out[:k]


def interest_embedding_texts(
    profile: dict[str, Any],
    now: datetime | None = None,
    k: int = 10,
) -> list[str]:
    """
    Rich natural-language strings for the signal-engine user_vector. Prefers
    specific subjects (sharper than the old free-text interests), then fills with
    category labels, then legacy keys. Never emits raw slugs.
    """
    now = now or datetime.now(UTC)
    texts: list[str] = list(top_interest_subjects(profile, now, k))
    if len(texts) < k:
        for slug, _, _ in _ranked_categories(profile, now):
            label = category_label(slug)
            if label not in texts:
                texts.append(label)
            if len(texts) >= k:
                break
    if not texts:
        texts = _legacy_top_keys(profile, k)
    return texts[:k]


def category_count(profile: dict[str, Any]) -> int:
    """Number of categories present in the new structure (used to sunset legacy)."""
    interests = profile.get("interests")
    return len(interests) if isinstance(interests, dict) else 0


def active_category_slugs(profile: dict[str, Any], now: datetime | None = None) -> list[str]:
    """Taxonomy slugs the user has a live (decayed weight > 0) interest in.

    Used by the signal-engine scoring gate to build a user's category allow-list.
    These are already taxonomy slugs (the only thing the interests map stores), so
    the caller does not need to normalise them. Ordered strongest-first.
    """
    now = now or datetime.now(UTC)
    return [slug for slug, _weight, _node in _ranked_categories(profile, now)]


def seed_onboarding_interests(
    interests: dict[str, Any],
    slugs: list[str],
    now: datetime | None = None,
) -> None:
    """Seed declared onboarding interests into the `interests` map in place.

    Each slug is folded in via apply_interest_signal with origin="onboarding" and
    no subject (a declared category has no specific subject yet). Off-taxonomy
    slugs coerce to OTHER, matching the writer's contract elsewhere.
    """
    now = now or datetime.now(UTC)
    for slug in slugs:
        apply_interest_signal(
            interests, slug, subject=None, now=now, origin=INTEREST_ORIGIN_ONBOARDING,
        )


# --------------------------------------------------------------------------
# Storylines — the narrative layer (reflection-tier owned)
# --------------------------------------------------------------------------
#
# A storyline is what is GOING ON in the user's life, fused from a whole session,
# not a single message. It carries the connected meaning a flat interest cannot:
# "wrote a blog on tensor parallelism to land an SDE role at Annapurna Labs (AWS)"
# instead of two disconnected chips "tensor parallelism" + "Annapurna Labs".
#
# Stored shape on UserAura/{uid}:
#
#   "storylines": {
#       "<id>": {
#           "summary":    str,                # the narrative one-liner shown to the user
#           "entities":   [str],              # named things in it
#           "categories": [taxonomy slug],
#           "intent":     str | None,         # e.g. "career_goal", "event_follow"
#           "kind":       "durable|event_driven|goal_instrumental",
#           "confidence": float,              # 0..1
#           "weight": float, "first_seen": iso, "last_seen": iso,
#       }
#   }
#
# Decay reuses the kind-aware half-life, so an event_driven storyline ("wants World
# Cup updates") fades on its own in about a week without any sweep job.


def apply_storyline(
    storylines: dict[str, Any],
    storyline_id: str,
    summary: str,
    entities: list[str],
    categories: list[str],
    intent: str | None,
    kind: str,
    confidence: float,
    now: datetime,
) -> None:
    """Insert or update one storyline in place (decay-then-increment its weight).

    `storyline_id` is a stable slug the reflection tier supplies (derived from the
    goal/entities) so the same ongoing storyline MERGES across sessions instead of
    fragmenting into near-duplicates. Off-taxonomy kinds coerce to durable.
    """
    key = sanitize_firestore_key(storyline_id)
    clean_summary = (summary or "").strip()
    if not key or not clean_summary:
        return
    safe_kind = kind if kind in INTEREST_KINDS else DEFAULT_INTEREST_KIND
    now_iso = now.isoformat()

    node = storylines.get(key)
    if not isinstance(node, dict):
        node = {"weight": 0.0, "first_seen": now_iso, "last_seen": now_iso}
        storylines[key] = node
    node["weight"] = decayed_weight_by_kind(
        node.get("weight"), node.get("last_seen"), node.get("kind"), now
    ) + 1.0
    node["last_seen"] = now_iso
    node["summary"] = clean_summary
    node["entities"] = [e.strip() for e in entities if isinstance(e, str) and e.strip()][:8]
    node["categories"] = [c for c in categories if c in INTEREST_CATEGORIES][:4]
    node["intent"] = intent
    node["kind"] = safe_kind
    try:
        node["confidence"] = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        node["confidence"] = 0.0
    node.setdefault("first_seen", now_iso)

    _cap_storylines(storylines, now)


def _cap_storylines(storylines: dict[str, Any], now: datetime) -> None:
    if len(storylines) <= MAX_STORYLINES:
        return
    ranked = sorted(
        storylines.items(),
        key=lambda kv: decayed_weight_by_kind(
            kv[1].get("weight"), kv[1].get("last_seen"), kv[1].get("kind"), now
        ) if isinstance(kv[1], dict) else 0.0,
        reverse=True,
    )
    for stale_key, _ in ranked[MAX_STORYLINES:]:
        storylines.pop(stale_key, None)


def ranked_storylines(
    profile: dict[str, Any],
    now: datetime | None = None,
    k: int = 6,
) -> list[dict[str, Any]]:
    """Live storylines (decayed weight > 0), strongest first, for prompts / the Aura screen."""
    now = now or datetime.now(UTC)
    raw = profile.get("storylines")
    if not isinstance(raw, dict):
        return []
    rows: list[tuple[float, dict[str, Any]]] = []
    for node in raw.values():
        if not isinstance(node, dict):
            continue
        weight = decayed_weight_by_kind(node.get("weight"), node.get("last_seen"), node.get("kind"), now)
        if weight > 0:
            rows.append((weight, node))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [node for _, node in rows[:k]]


def storyline_prompt_lines(
    profile: dict[str, Any],
    now: datetime | None = None,
    k: int = 4,
) -> list[str]:
    """Narrative summaries for the chat / voice system prompt."""
    return [
        str(node["summary"]).strip()
        for node in ranked_storylines(profile, now, k)
        if node.get("summary")
    ]


def reclassify_interest_kind(
    interests: dict[str, Any],
    category: str,
    subject: str,
    kind: str,
) -> bool:
    """Reflection merge-op: set the kind on an EXISTING flat interest subject so its
    decay changes (e.g. mark "FIFA World Cup" event_driven so it fades in a week
    instead of looking like a standing football obsession). Returns True if applied.

    Matches the subject the capture tier wrote. Capture keys subjects by raw display
    today (the case-insensitive dedupe is a separate, deferred change), so this tries
    an exact key, then a sanitized-casefold key, then a case-insensitive display scan.
    A no-op (returns False) when the subject isn't present — capture may not have
    written it, which is fine; the storyline still carries the kind.
    """
    if kind not in INTEREST_KINDS:
        return False
    node = interests.get(category)
    if not isinstance(node, dict):
        return False
    subjects = node.get("subjects")
    if not isinstance(subjects, dict):
        return False

    wanted = " ".join((subject or "").split())
    target = subjects.get(subject) or subjects.get(sanitize_firestore_key(wanted.casefold()))
    if not isinstance(target, dict):
        for sval in subjects.values():
            if isinstance(sval, dict) and str(sval.get("display", "")).casefold() == wanted.casefold():
                target = sval
                break
    if not isinstance(target, dict):
        return False
    target["kind"] = kind
    return True


def prune_interest(interests: dict[str, Any], category: str, subject: str) -> bool:
    """Reflection cleanup: remove a mis-attributed subject from a category — most often a
    thing the fast layer stored as the user's interest that turned out to be about someone
    else (a gift, a question on a friend's behalf). Drops the category entirely if it has
    no subjects left. Returns True if something was removed. Matches the capture-tier
    subject by exact key or case-insensitive display, like reclassify_interest_kind."""
    node = interests.get(category)
    if not isinstance(node, dict):
        return False
    subjects = node.get("subjects")
    if not isinstance(subjects, dict) or not subjects:
        return False

    wanted = " ".join((subject or "").split()).casefold()
    target_key: str | None = None
    for sk, sval in subjects.items():
        display = str(sval.get("display", sk)) if isinstance(sval, dict) else str(sk)
        if str(sk).casefold() == wanted or display.casefold() == wanted:
            target_key = sk
            break
    if target_key is None:
        return False

    subjects.pop(target_key, None)
    if not subjects:
        interests.pop(category, None)
    return True


# --------------------------------------------------------------------------
# Traits — corroborated personality signals (reflection-tier owned)
# --------------------------------------------------------------------------
#
# Stored shape:
#   "traits": {
#       "<sanitized_key>": {
#           "display": str, "weight": float, "confidence": float,
#           "evidence_count": int, "first_seen": iso, "last_seen": iso,
#       }
#   }
#
# A trait is STORED on every inference but only SHOWN once corroborated
# (evidence_count >= TRAIT_MIN_EVIDENCE AND confidence >= TRAIT_MIN_CONFIDENCE), so a
# single eager/flattering inference can never become a visible permanent label. The
# user reads their own Aura, so a wrong "anxious" shown back is a trust hit. Traits
# decay like everything else (a phase isn't forever); promotion is read-time, never
# stored, so thresholds can change without a migration.


def _trait_evidence(node: dict[str, Any]) -> int:
    """Corroboration count = number of DISTINCT sessions that evidenced the trait. Falls
    back to the legacy integer `evidence_count` for nodes written before session tracking."""
    sessions = node.get("sessions")
    if isinstance(sessions, list) and sessions:
        return len(sessions)
    return int(node.get("evidence_count", 0) or 0)


def apply_trait(
    traits: dict[str, Any],
    name: str,
    confidence: float,
    now: datetime,
    session_id: str | None = None,
) -> None:
    """Fold one inferred trait into the map in place: decay-then-increment its weight,
    record corroboration, and keep the strongest confidence seen.

    Corroboration counts DISTINCT sessions, not re-runs: re-reflecting the same growing
    session must never inflate a trait toward the shown threshold. When `session_id` is
    given it is added to a capped set (idempotent); when absent (legacy/test calls) a plain
    counter is bumped instead, so both shapes work and old docs keep counting."""
    clean = " ".join((name or "").split())
    if not clean or len(clean) > 60:
        return
    key = sanitize_firestore_key(clean.casefold())
    if not key:
        return
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.0
    now_iso = now.isoformat()

    node = traits.get(key)
    if not isinstance(node, dict):
        node = {
            "display": clean, "weight": 0.0, "confidence": 0.0,
            "first_seen": now_iso, "last_seen": now_iso,
        }
        traits[key] = node
    node["weight"] = decayed_weight_by_kind(node.get("weight"), node.get("last_seen"), None, now) + 1.0
    node["confidence"] = max(conf, float(node.get("confidence", 0.0) or 0.0))
    node["display"] = clean
    node["last_seen"] = now_iso
    node.setdefault("first_seen", now_iso)

    if session_id:
        sessions = node.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        if session_id not in sessions:
            sessions.append(session_id)
        node["sessions"] = sessions[-MAX_TRAIT_SESSIONS:]
    else:
        node["evidence_count"] = int(node.get("evidence_count", 0) or 0) + 1

    _cap_traits(traits, now)


def _cap_traits(traits: dict[str, Any], now: datetime) -> None:
    if len(traits) <= MAX_TRAITS:
        return
    ranked = sorted(
        traits.items(),
        key=lambda kv: decayed_weight_by_kind(
            kv[1].get("weight"), kv[1].get("last_seen"), None, now
        ) if isinstance(kv[1], dict) else 0.0,
        reverse=True,
    )
    for stale_key, _ in ranked[MAX_TRAITS:]:
        traits.pop(stale_key, None)


def shown_traits(profile: dict[str, Any], now: datetime | None = None, k: int = 6) -> list[str]:
    """Corroborated, still-live trait displays for the Aura screen / prompt. Gated by
    TRAIT_MIN_EVIDENCE + TRAIT_MIN_CONFIDENCE so an uncorroborated guess never shows."""
    now = now or datetime.now(UTC)
    raw = profile.get("traits")
    if not isinstance(raw, dict):
        return []
    rows: list[tuple[float, str]] = []
    for node in raw.values():
        if not isinstance(node, dict):
            continue
        if _trait_evidence(node) < TRAIT_MIN_EVIDENCE:
            continue
        if float(node.get("confidence", 0.0) or 0.0) < TRAIT_MIN_CONFIDENCE:
            continue
        weight = decayed_weight_by_kind(node.get("weight"), node.get("last_seen"), None, now)
        if weight > 0 and node.get("display"):
            rows.append((weight, str(node["display"])))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [name for _, name in rows[:k]]
