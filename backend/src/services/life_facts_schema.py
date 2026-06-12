"""
life_facts schema — the single source of truth for the sparse, typed map of
durable life facts Buddy learns passively from chat (a pet's name, the user's
city, whether they work out). These facts *arm* the Icebreaker / Moments engine:
a "how's Bruno?" opener only ever fires when a `has_pet` fact exists.

This module is the one place the field-name contract for life facts lives, so a
rename can never silently break the writer (the chat extractor) or the readers
(the icebreaker context bundle). It mirrors `user_aura_schema.py`'s discipline:
both sides of the contract import these constants and accessors instead of
hard-coding the strings (see CLAUDE.md data-layer discipline).

Why a CLOSED key set and not a free-form map: the interest taxonomy used to be
free text and fragmented into 100+ near-duplicate buckets (lessons-learnt,
2026-06-04). Life facts repeat that risk only if the *keys* are free text, so the
keys here are a fixed tuple; the LLM-supplied value is data (the pet's name), not
a key. An off-list key is dropped, never coerced into a junk bucket.

Stored shape on `UserAura/{uid}`:

    "life_facts": {
        "has_pet":   { "value": "dog named Bruno", "first_seen": iso, "last_seen": iso },
        "home_city": { "value": "Hyderabad",       "first_seen": iso, "last_seen": iso },
        ...
    }

`first_seen` / `last_seen` exist so a reader can enforce a minimum-dwell rule —
never reference a fact within MIN_FACT_DWELL of first learning it, which is the
difference between "a friend who remembers" and "creepy" (see read accessor).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# ── The closed set of life-fact keys (the contract) ─────────────────────────
# Add a key here (and teach the extractor prompt about it) to learn a new kind of
# fact. Keep the set small and high-signal — every key is something Buddy could
# warmly reference. An LLM-emitted key outside this set is dropped on write.
LIFE_FACT_HAS_PET = "has_pet"
LIFE_FACT_HOME_CITY = "home_city"
LIFE_FACT_HOME_COUNTRY = "home_country"
LIFE_FACT_WORKS_OUT = "works_out"
LIFE_FACT_COMMUTE_MODE = "commute_mode"
LIFE_FACT_IMPORTANT_DATE = "important_date"
LIFE_FACT_RELATIONSHIP_STATUS = "relationship_status"
LIFE_FACT_OCCUPATION = "occupation"
LIFE_FACT_DIETARY_PREF = "dietary_pref"

LIFE_FACT_KEYS: tuple[str, ...] = (
    LIFE_FACT_HAS_PET,
    LIFE_FACT_HOME_CITY,
    LIFE_FACT_HOME_COUNTRY,
    LIFE_FACT_WORKS_OUT,
    LIFE_FACT_COMMUTE_MODE,
    LIFE_FACT_IMPORTANT_DATE,
    LIFE_FACT_RELATIONSHIP_STATUS,
    LIFE_FACT_OCCUPATION,
    LIFE_FACT_DIETARY_PREF,
)

# Human-readable hint per key, fed to the extraction prompt so the model knows
# what each bucket means. Keep terse — it goes into the system prompt verbatim.
LIFE_FACT_DESCRIPTIONS: dict[str, str] = {
    LIFE_FACT_HAS_PET: "a pet the user owns, with its kind and name if given (e.g. 'dog named Bruno')",
    LIFE_FACT_HOME_CITY: "the city/town the user lives in",
    LIFE_FACT_HOME_COUNTRY: "the country the user lives in",
    LIFE_FACT_WORKS_OUT: "a regular fitness habit (e.g. 'runs in the mornings', 'goes to the gym')",
    LIFE_FACT_COMMUTE_MODE: "how the user usually gets around (e.g. 'drives', 'takes the metro')",
    LIFE_FACT_IMPORTANT_DATE: "a recurring personal date with what it is (e.g. 'birthday on Aug 1')",
    LIFE_FACT_RELATIONSHIP_STATUS: "stable relationship status (e.g. 'married', 'has a partner')",
    LIFE_FACT_OCCUPATION: "what the user does for work (e.g. 'software engineer')",
    LIFE_FACT_DIETARY_PREF: "a durable dietary preference (e.g. 'vegetarian', 'no caffeine')",
}

# The field name on the UserAura doc. One place so writer and readers agree.
LIFE_FACTS_FIELD = "life_facts"

# Defence against runaway LLM output — a fact value is a short phrase, not prose.
MAX_LIFE_FACT_VALUE_LENGTH = 120

# Minimum time a fact must have existed before a reader may surface it. Referencing
# a fact the user mentioned an hour ago feels like surveillance; a day later it
# feels like a friend who remembered. Tune here, read by `read_life_facts_for_arming`.
MIN_FACT_DWELL = timedelta(hours=24)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def apply_life_fact(
    life_facts: dict[str, Any],
    key: str,
    value: str | None,
    now: datetime,
) -> None:
    """Fold one (key, value) life fact into the `life_facts` map in place.

    Off-taxonomy keys and empty/oversized values are dropped (not coerced) — the
    closed key set is the whole point. The newest value wins, but `first_seen` is
    preserved so the minimum-dwell rule measures from when we FIRST learned it.
    """
    if key not in LIFE_FACT_KEYS:
        return
    clean = (value or "").strip()
    if not clean or len(clean) > MAX_LIFE_FACT_VALUE_LENGTH:
        return

    now_iso = now.isoformat()
    node = life_facts.get(key)
    if not isinstance(node, dict):
        node = {"value": clean, "first_seen": now_iso, "last_seen": now_iso}
        life_facts[key] = node
        return
    node["value"] = clean
    node["last_seen"] = now_iso
    node.setdefault("first_seen", now_iso)


def remove_life_fact(life_facts: dict[str, Any], key: str) -> None:
    """Delete a life fact the user has explicitly denied or corrected in chat
    (e.g. "I don't have a dog", "I moved out of Hyderabad"). A no-op for an
    off-taxonomy key or one never stored — a correction must never raise. Clearing
    on denial is the difference between a friend who listens and one who clings to
    a wrong assumption (and then opens with "how's your dog?" about a pet you never
    had — see the Icebreaker engine's life-aware openers)."""
    if key in LIFE_FACT_KEYS:
        life_facts.pop(key, None)


def has_life_facts(profile: dict[str, Any]) -> bool:
    """True if the profile carries any stored life fact at all."""
    facts = profile.get(LIFE_FACTS_FIELD)
    return isinstance(facts, dict) and len(facts) > 0


def read_life_facts_for_arming(
    profile: dict[str, Any],
    now: datetime | None = None,
    min_dwell: timedelta = MIN_FACT_DWELL,
) -> dict[str, str]:
    """The life facts old enough to safely reference, as a flat {key: value} map.

    Facts learned within `min_dwell` are withheld (the anti-creepiness rule). A
    fact missing a parseable `first_seen` is treated as old enough (it predates
    the timestamp contract) rather than withheld forever.
    """
    now = now or datetime.now(UTC)
    facts = profile.get(LIFE_FACTS_FIELD)
    if not isinstance(facts, dict):
        return {}

    out: dict[str, str] = {}
    for key, node in facts.items():
        if key not in LIFE_FACT_KEYS or not isinstance(node, dict):
            continue
        value = str(node.get("value", "")).strip()
        if not value:
            continue
        first_seen = _parse_iso(node.get("first_seen"))
        if first_seen is not None and (now - first_seen) < min_dwell:
            continue  # too fresh to surface — would feel like surveillance
        out[key] = value
    return out
