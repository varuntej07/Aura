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
            "weight": float,            # time-decayed score (see HALF_LIFE_DAYS)
            "first_seen": iso8601,
            "last_seen":  iso8601,
            "subjects": {
                "<sanitized_key>": {
                    "display": str,     # original casing, e.g. "KCR"
                    "weight": float,
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

# Recency half-life: a node's weight halves every this-many days of inactivity.
HALF_LIFE_DAYS = 30.0
# Per-category cap on distinct subjects; lowest-weight subjects are evicted.
MAX_SUBJECTS_PER_CATEGORY = 15
# Max subject string length we will store (defence against runaway LLM output).
MAX_SUBJECT_LENGTH = 80

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


def _decayed_weight(weight: Any, last_seen: Any, now: datetime) -> float:
    """Stored weight decayed forward to `now`. Returns 0.0 for unusable input."""
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
    return base * (0.5 ** (days / HALF_LIFE_DAYS))


# --------------------------------------------------------------------------
# Writer primitive
# --------------------------------------------------------------------------

def apply_interest_signal(
    interests: dict[str, Any],
    category: str,
    subject: str | None,
    now: datetime,
) -> None:
    """
    Fold one (category, subject) signal into the `interests` map in place.

    Unknown categories are coerced to OTHER_CATEGORY. The category weight always
    advances; the subject weight advances only when a concrete subject is given.
    Both use decay-then-increment so the stored number is recency-aware.
    """
    slug = category if category in INTEREST_CATEGORIES else OTHER_CATEGORY
    now_iso = now.isoformat()

    node = interests.get(slug)
    if not isinstance(node, dict):
        node = {"weight": 0.0, "first_seen": now_iso, "last_seen": now_iso, "subjects": {}}
        interests[slug] = node
    node["weight"] = _decayed_weight(node.get("weight"), node.get("last_seen"), now) + 1.0
    node["last_seen"] = now_iso
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
    snode["weight"] = _decayed_weight(snode.get("weight"), snode.get("last_seen"), now) + 1.0
    snode["last_seen"] = now_iso
    snode["display"] = clean
    snode.setdefault("first_seen", now_iso)

    _cap_subjects(subjects, now)


def _cap_subjects(subjects: dict[str, Any], now: datetime) -> None:
    if len(subjects) <= MAX_SUBJECTS_PER_CATEGORY:
        return
    ranked = sorted(
        subjects.items(),
        key=lambda kv: _decayed_weight(kv[1].get("weight"), kv[1].get("last_seen"), now),
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
        weight = _decayed_weight(node.get("weight"), node.get("last_seen"), now)
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
        key=lambda s: _decayed_weight(s.get("weight"), s.get("last_seen"), now) if isinstance(s, dict) else 0.0,
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
                    scored.append((str(s["display"]), _decayed_weight(s.get("weight"), s.get("last_seen"), now)))
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
