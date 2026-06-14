"""Resolve a user's region for the on-demand world briefing's "one local item".

The user's IANA ``timezone`` (on ``users/{uid}``) is the only region signal reliably
present on every account — ``locale`` is written by the client but is empty for most
users today — so it is the source here. We map the timezone to an ISO-3166 alpha-2
country code (the per-region cache key) plus a human country label woven into the
grounded prompt ("closer to home in India").

The map is intentionally a curated set of the timezones the user base actually spans
rather than an exhaustive IANA→country table: an unrecognised zone returns the GLOBAL
sentinel, which makes the briefing degrade safely to global-only (3-4 world stories, no
local one) instead of guessing a wrong country. Extend ``_ZONE_TO_REGION`` as the user
base grows — adding a row is the whole change.
"""

from __future__ import annotations

from dataclasses import dataclass

# Sentinel region: no resolvable country → the briefing skips the local item and runs
# global-only. country_code doubles as the per-region cache key, so a stable literal.
GLOBAL_REGION_CODE = "GLOBAL"


@dataclass(frozen=True)
class WorldRegion:
    """Where the user is, for the "one local story" slot and the cache key."""

    # ISO-3166 alpha-2 (e.g. "IN") or GLOBAL_REGION_CODE. Used as the cache key.
    country_code: str
    # Human label woven into the prompt ("India", "the US"). Empty for GLOBAL.
    country_name: str

    @property
    def is_global(self) -> bool:
        return self.country_code == GLOBAL_REGION_CODE


_GLOBAL = WorldRegion(country_code=GLOBAL_REGION_CODE, country_name="")

# Exact IANA timezone → (ISO-3166 alpha-2, human label). Curated for the actual /
# expected user base; unknown zones fall through to GLOBAL (global-only briefing).
_ZONE_TO_REGION: dict[str, tuple[str, str]] = {
    # India
    "Asia/Kolkata": ("IN", "India"),
    "Asia/Calcutta": ("IN", "India"),
    # United States (one label; the local search is country-level, not city-level)
    "America/New_York": ("US", "the US"),
    "America/Detroit": ("US", "the US"),
    "America/Chicago": ("US", "the US"),
    "America/Denver": ("US", "the US"),
    "America/Phoenix": ("US", "the US"),
    "America/Los_Angeles": ("US", "the US"),
    "America/Anchorage": ("US", "the US"),
    "Pacific/Honolulu": ("US", "the US"),
    # United Kingdom / Ireland
    "Europe/London": ("GB", "the UK"),
    "Europe/Dublin": ("IE", "Ireland"),
    # Canada
    "America/Toronto": ("CA", "Canada"),
    "America/Vancouver": ("CA", "Canada"),
    "America/Edmonton": ("CA", "Canada"),
    "America/Winnipeg": ("CA", "Canada"),
    "America/Halifax": ("CA", "Canada"),
    # Australia / New Zealand
    "Australia/Sydney": ("AU", "Australia"),
    "Australia/Melbourne": ("AU", "Australia"),
    "Australia/Brisbane": ("AU", "Australia"),
    "Australia/Perth": ("AU", "Australia"),
    "Australia/Adelaide": ("AU", "Australia"),
    "Pacific/Auckland": ("NZ", "New Zealand"),
    # Europe
    "Europe/Paris": ("FR", "France"),
    "Europe/Berlin": ("DE", "Germany"),
    "Europe/Madrid": ("ES", "Spain"),
    "Europe/Rome": ("IT", "Italy"),
    "Europe/Amsterdam": ("NL", "the Netherlands"),
    "Europe/Lisbon": ("PT", "Portugal"),
    "Europe/Stockholm": ("SE", "Sweden"),
    "Europe/Zurich": ("CH", "Switzerland"),
    "Europe/Warsaw": ("PL", "Poland"),
    "Europe/Moscow": ("RU", "Russia"),
    "Europe/Istanbul": ("TR", "Turkey"),
    # Middle East
    "Asia/Dubai": ("AE", "the UAE"),
    "Asia/Riyadh": ("SA", "Saudi Arabia"),
    "Asia/Jerusalem": ("IL", "Israel"),
    "Asia/Qatar": ("QA", "Qatar"),
    # Asia
    "Asia/Singapore": ("SG", "Singapore"),
    "Asia/Tokyo": ("JP", "Japan"),
    "Asia/Seoul": ("KR", "South Korea"),
    "Asia/Shanghai": ("CN", "China"),
    "Asia/Hong_Kong": ("HK", "Hong Kong"),
    "Asia/Bangkok": ("TH", "Thailand"),
    "Asia/Jakarta": ("ID", "Indonesia"),
    "Asia/Manila": ("PH", "the Philippines"),
    "Asia/Karachi": ("PK", "Pakistan"),
    "Asia/Dhaka": ("BD", "Bangladesh"),
    "Asia/Colombo": ("LK", "Sri Lanka"),
    # Africa
    "Africa/Lagos": ("NG", "Nigeria"),
    "Africa/Cairo": ("EG", "Egypt"),
    "Africa/Johannesburg": ("ZA", "South Africa"),
    "Africa/Nairobi": ("KE", "Kenya"),
    # Latin America
    "America/Sao_Paulo": ("BR", "Brazil"),
    "America/Mexico_City": ("MX", "Mexico"),
    "America/Bogota": ("CO", "Colombia"),
    "America/Buenos_Aires": ("AR", "Argentina"),
    "America/Argentina/Buenos_Aires": ("AR", "Argentina"),
    "America/Santiago": ("CL", "Chile"),
    "America/Lima": ("PE", "Peru"),
}


def resolve_region(timezone_name: str | None) -> WorldRegion:
    """Map an IANA timezone to a :class:`WorldRegion`. Unknown / missing / "UTC" →
    the GLOBAL sentinel, so the briefing degrades to global-only rather than guessing
    a wrong country. Matching is exact on the IANA name (the form Flutter stores)."""
    if not timezone_name:
        return _GLOBAL
    hit = _ZONE_TO_REGION.get(timezone_name.strip())
    if hit is None:
        return _GLOBAL
    code, name = hit
    return WorldRegion(country_code=code, country_name=name)
