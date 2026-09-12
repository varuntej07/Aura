"""Turn a user's Aura profile into the safe facet bundle a weekly issue is built from.

PURE — no I/O, no LLM, no Firestore. Takes an already-fetched profile dict and
returns values, so the whole thing is inspectable with a throwaway ``python -c``.

**This module is the ONLY reader of the Aura profile for generation.** That is a
deliberate structural property, not a convention: everything the prose model
could ever see about a user passes through :func:`build_mix_facets`, so the
exclusions below cannot be bypassed by a future caller reaching around it.

Three exclusions, all structural — never a word list over what the user said
(CLAUDE.md:137). Every filter reads a machine-assigned label from a closed
vocabulary or a boolean another subsystem already computed:

1. **Sensitive categories.** Interest categories and storylines tagged with any
   of ``SENSITIVE_CATEGORY_SLUGS`` are dropped. The Aura interest taxonomy is a
   closed 30-slug set (``user_aura_schema.CATEGORY_LABELS``) and four of the seven
   sensitive slugs live in it directly.
2. **Graph-flagged entities.** Facets whose entity the memory graph already marked
   ``inferred_sensitive`` are dropped. This covers the sensitive slugs the
   interest taxonomy cannot express (grief, trauma, gender identity).
3. **A key whitelist.** The sanitized profile copy carries ONLY the keys listed in
   :data:`_ALLOWED_PROFILE_KEYS`. So ``emotional_signals``, ``urgency_distribution``,
   ``tone_signals`` and every other behavioural frequency map are unreachable —
   and a field added to UserAura tomorrow cannot silently start flowing into
   generated prose. Do not "helpfully" widen this set: inferring a user's distress
   and then writing them a story about it is the specific outcome it prevents.

The approach is to sanitize the profile FIRST and then call the stock
``user_aura_schema`` accessors on the copy, rather than filtering their output.
The accessors return flat display strings that have already lost category
attribution, so post-filtering is impossible; sanitizing first also means all the
kind-aware time decay and ranking logic is reused rather than reimplemented.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..life_facts_schema import LIFE_FACTS_FIELD, read_life_facts_for_arming
from ..threads.sensitivity import SENSITIVE_CATEGORY_SLUGS
from ..user_aura_schema import (
    ranked_storylines,
    shown_traits,
    top_interest_subjects,
)

# Only these profile keys reach generation. See exclusion 3 above.
_ALLOWED_PROFILE_KEYS = frozenset(
    {"interests", "storylines", "traits", "inferred_goals", "explicit_facts", LIFE_FACTS_FIELD}
)

# `life_facts` keys are a closed set of 9 (`life_facts_schema.LIFE_FACT_KEYS`), so
# each is reviewed individually instead of being dropped wholesale for lacking a
# category. Excluded deliberately:
#   relationship_status — maps onto the `relationships_social` sensitive slug.
#   dietary_pref        — "no caffeine" can encode a medical condition.
#   important_date      — a story that names someone's birthday reads as surveillance,
#                         and it adds nothing to prose.
_ALLOWED_LIFE_FACT_KEYS = frozenset(
    {"has_pet", "home_city", "home_country", "works_out", "commute_mode", "occupation"}
)

# Below this the issue would be generic filler dressed up as personal, which is
# worse than the curated catalog. Counted across every facet kind.
MIN_FACETS = 6
MIN_DISTINCT_CATEGORIES = 2

# How much of each kind reaches the prompt.
MAX_STORYLINES = 5
MAX_TRAITS = 4
MAX_SUBJECTS = 6
MAX_GOALS = 4


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:8]


def _is_sensitive_categories(categories: Any) -> bool:
    """True if any assigned category slug is sensitive. Closed-vocabulary set
    intersection over an extractor-assigned label — no user speech inspected."""
    if not isinstance(categories, (list, tuple, set)):
        return False
    return any(
        isinstance(slug, str) and slug.strip().lower() in SENSITIVE_CATEGORY_SLUGS
        for slug in categories
    )


def _entities_of(node: dict[str, Any]) -> list[str]:
    raw = node.get("entities")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(entity).strip() for entity in raw if str(entity).strip()]


def sanitize_profile(
    profile: dict[str, Any],
    *,
    sensitive_entity_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """A copy of the profile carrying only non-sensitive, whitelisted material.

    ``sensitive_entity_keys`` comes from the memory graph's ``inferred_sensitive``
    flags (see ``threads.sensitivity.read_graph_sensitivity_nodes``). Matching is
    on normalized entity keys the graph itself assigned, not on free text.
    """
    if not isinstance(profile, dict):
        return {}

    blocked = {key.strip().lower() for key in sensitive_entity_keys if key}
    clean: dict[str, Any] = {}

    interests = profile.get("interests")
    if isinstance(interests, dict):
        clean["interests"] = {
            slug: node
            for slug, node in interests.items()
            if isinstance(node, dict) and str(slug).strip().lower() not in SENSITIVE_CATEGORY_SLUGS
        }

    storylines = profile.get("storylines")
    if isinstance(storylines, dict):
        kept: dict[str, Any] = {}
        for key, node in storylines.items():
            if not isinstance(node, dict):
                continue
            if _is_sensitive_categories(node.get("categories")):
                continue
            if blocked and any(entity.lower() in blocked for entity in _entities_of(node)):
                continue
            kept[key] = node
        clean["storylines"] = kept

    traits = profile.get("traits")
    if isinstance(traits, dict):
        # Traits carry no category, but `shown_traits` already requires >=2
        # distinct sessions and >=0.7 confidence, so an eager one-off inference
        # cannot reach a prompt through here.
        clean["traits"] = traits

    goals = profile.get("inferred_goals")
    if isinstance(goals, list):
        clean["inferred_goals"] = [str(goal).strip() for goal in goals if str(goal).strip()]

    facts = profile.get(LIFE_FACTS_FIELD)
    if isinstance(facts, dict):
        clean[LIFE_FACTS_FIELD] = {
            key: node for key, node in facts.items() if key in _ALLOWED_LIFE_FACT_KEYS
        }

    # `explicit_facts` is free text the user stated about themselves, with no
    # category to filter on. Fail closed: omitted entirely.

    # The whitelist is ENFORCED here, not merely documented: a key added to
    # `clean` above (or to UserAura tomorrow) that is not on the list is dropped
    # rather than quietly reaching the prose model. This is what makes
    # "emotional_signals is unreachable" a property of the code instead of a
    # promise in a comment.
    return {key: value for key, value in clean.items() if key in _ALLOWED_PROFILE_KEYS}


@dataclass
class MixFacets:
    """The complete, safe view of a user that reaches the prose model."""

    storylines: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    life_facts: dict[str, str] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    digest: list[str] = field(default_factory=list)
    """Slugs and hashes ONLY — stored on the mix doc for auditability without
    making it a second copy of the private profile under a different retention
    policy."""

    @property
    def total(self) -> int:
        return (
            len(self.storylines)
            + len(self.traits)
            + len(self.subjects)
            + len(self.goals)
            + len(self.life_facts)
        )

    @property
    def is_sufficient(self) -> bool:
        """Enough signal to write something genuinely personal?

        A structural gate on collected state — not a judgement about the user.
        Below it the caller serves the curated catalog, which is a better
        experience than six generically 'personal' stories.
        """
        return self.total >= MIN_FACETS and len(self.categories) >= MIN_DISTINCT_CATEGORIES

    def insufficient_reason(self) -> str:
        if self.total < MIN_FACETS:
            return f"thin_signal_facets:{self.total}<{MIN_FACETS}"
        if len(self.categories) < MIN_DISTINCT_CATEGORIES:
            return f"thin_signal_categories:{len(self.categories)}<{MIN_DISTINCT_CATEGORIES}"
        return ""


def build_mix_facets(
    profile: dict[str, Any],
    *,
    now: datetime | None = None,
    sensitive_entity_keys: frozenset[str] = frozenset(),
) -> MixFacets:
    """The single entry point from a raw Aura profile to generation input."""
    now = now or datetime.now(UTC)
    clean = sanitize_profile(profile, sensitive_entity_keys=sensitive_entity_keys)
    if not clean:
        return MixFacets()

    storyline_nodes = ranked_storylines(clean, now, MAX_STORYLINES)
    storylines = [
        str(node["summary"]).strip() for node in storyline_nodes if str(node.get("summary") or "").strip()
    ]
    traits = shown_traits(clean, now, MAX_TRAITS)
    subjects = top_interest_subjects(clean, now, MAX_SUBJECTS)
    goals = [str(goal) for goal in clean.get("inferred_goals", [])][:MAX_GOALS]

    # Reuses the 24h anti-creepiness dwell rule: a fact learned yesterday is
    # withheld, which is the difference between a friend who remembers and
    # surveillance (`life_facts_schema.read_life_facts_for_arming`).
    life_facts = {
        key: value
        for key, value in read_life_facts_for_arming(clean, now).items()
        if key in _ALLOWED_LIFE_FACT_KEYS
    }

    categories = sorted(
        {
            str(slug).strip().lower()
            for slug in clean.get("interests", {})
            if str(slug).strip()
        }
        | {
            str(slug).strip().lower()
            for node in storyline_nodes
            for slug in (node.get("categories") or [])
            if str(slug).strip()
        }
    )

    digest = (
        [f"storyline:{_digest(summary)}" for summary in storylines]
        + [f"trait:{_digest(trait)}" for trait in traits]
        + [f"interest:{slug}" for slug in categories]
        + [f"goal:{_digest(goal)}" for goal in goals]
        + [f"fact:{key}" for key in sorted(life_facts)]
    )

    return MixFacets(
        storylines=storylines,
        traits=traits,
        subjects=subjects,
        goals=goals,
        life_facts=life_facts,
        categories=categories,
        digest=digest[:40],
    )
