"""Assemble the FREE context packet an icebreaker opener is built from.

Everything here comes from data we already have or free APIs — never paid live
web search (the RSS-first rule). Sources:
  * the user's region / language / season / weekday  (from their profile + clock)
  * today's weather                                   (Open-Meteo, free, fail-open)
  * headlines that match their interests              (the existing content pool)
  * life_facts + top interests                        (UserAura, learned passively)

Each external read is isolated: weather and headlines are fetched concurrently and
either failing just drops that field from the packet — one failure never blocks
the opener or another field.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..life_facts_schema import read_life_facts_for_arming
from ..signal_engine.content_pool import list_recent_candidates
from ..signal_engine.notification_framer import derive_local_time_band
from ..user_aura_schema import top_interest_subjects
from .icebreaker_store import UserTargeting
from .weather_provider import coordinates_for_timezone, fetch_today_weather

# How many interest-matched headlines to surface as candidate hooks.
_MAX_HEADLINES = 6
# Pull a larger recent pool, then keep only the headlines that intersect the
# user's interests. A bare global headline is newsletter spam, not a friend hook,
# so the prompt only ever sees headlines tied to something they actually follow.
_HEADLINE_FETCH_POOL = 24
# How many learned interest subjects to surface.
_MAX_INTEREST_SUBJECTS = 5


@dataclass
class IcebreakerContext:
    """The free context packet handed to the opener generator."""

    region_country: str          # "IN" | "US" | "" ...
    language: str
    weekday: str                 # "Monday" ...
    local_date: str              # "YYYY-MM-DD"
    time_band: str               # morning | midday | afternoon | evening | late
    season: str                  # winter | spring | summer | autumn | ""
    weather: str | None          # "hot and clear (33C)" or None
    headlines: list[str] = field(default_factory=list)
    life_facts: dict[str, str] = field(default_factory=dict)
    interest_subjects: list[str] = field(default_factory=list)
    recent_opener_topics: list[str] = field(default_factory=list)

    def has_any_hook(self) -> bool:
        """True if there is at least one thing worth opening about. With nothing —
        no weather, headline, fact, or interest — the engine should skip rather
        than force a hollow opener."""
        return bool(
            self.weather or self.headlines
            or self.life_facts or self.interest_subjects
        )


def _region_country_from_locale(locale: str) -> str:
    """Extract an uppercase country code from a locale like 'en-IN' / 'en_US'."""
    token = (locale or "").replace("_", "-")
    if "-" in token:
        return token.split("-", 1)[1].strip().upper()
    return ""


def _season_for(month: int, latitude: float | None) -> str:
    """Meteorological season from month, flipped for the southern hemisphere."""
    northern = [
        "", "winter", "winter", "spring", "spring", "spring", "summer",
        "summer", "summer", "autumn", "autumn", "autumn", "winter",
    ][month] if 1 <= month <= 12 else ""
    if not northern:
        return ""
    if latitude is not None and latitude < 0:
        flip = {"winter": "summer", "summer": "winter", "spring": "autumn", "autumn": "spring"}
        return flip.get(northern, northern)
    return northern


async def _read_user_aura(user_id: str) -> dict:
    def _fetch() -> dict:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("icebreaker.context_bundle: UserAura read failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return {}


def _headlines_matching_interests(
    titles: list[str], interest_subjects: list[str]
) -> list[str]:
    """Keep only headlines that mention one of the user's interest subjects.

    A bare global headline (from the shared content pool) reads like a newsletter,
    not a friend. We surface a headline only when it intersects something the user
    actually follows, matched as a whole-word set so a subject like "Lionel Messi"
    must appear in full (not "Messi" buried inside another word). No interests yet
    => no headlines, and the opener leans on weather or a personal fact (or skips).
    """
    subject_word_sets = [
        words
        for subject in interest_subjects
        if (words := set(re.findall(r"[a-z0-9]+", subject.lower())))
    ]
    if not subject_word_sets:
        return []
    matched: list[str] = []
    for title in titles:
        title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
        if any(words <= title_words for words in subject_word_sets):
            matched.append(title)
    return matched


async def build_context_bundle(
    user_id: str,
    targeting: UserTargeting,
    local_now: datetime,
    recent_opener_topics: list[str],
) -> IcebreakerContext:
    """Build the free context packet for one user. Never raises."""
    region_country = _region_country_from_locale(targeting.locale)
    coords = coordinates_for_timezone(targeting.timezone)
    latitude = coords[0] if coords else None

    # Fetch the three independent external/Firestore reads concurrently; isolate
    # each so one failure cannot take down the others (return_exceptions=True).
    aura, weather, headlines = await asyncio.gather(
        _read_user_aura(user_id),
        fetch_today_weather(targeting.timezone),
        list_recent_candidates(limit=_HEADLINE_FETCH_POOL, region=region_country),
        return_exceptions=True,
    )
    aura = aura if isinstance(aura, dict) else {}
    weather_summary = weather if (weather is not None and not isinstance(weather, BaseException)) else None
    headline_items = headlines if isinstance(headlines, list) else []

    interest_subjects = top_interest_subjects(aura, k=_MAX_INTEREST_SUBJECTS)
    relevant_headlines = _headlines_matching_interests(
        [h.title for h in headline_items], interest_subjects
    )[:_MAX_HEADLINES]

    return IcebreakerContext(
        region_country=region_country,
        language=targeting.language,
        weekday=local_now.strftime("%A"),
        local_date=local_now.date().isoformat(),
        time_band=derive_local_time_band(local_now),
        season=_season_for(local_now.month, latitude),
        weather=weather_summary.describe() if weather_summary else None,
        headlines=relevant_headlines,
        life_facts=read_life_facts_for_arming(aura, now=local_now),
        interest_subjects=interest_subjects,
        recent_opener_topics=list(recent_opener_topics or []),
    )
