"""Today's weather for a user, from Open-Meteo (free, no API key).

We never ask the user for their location. Coordinates are inferred coarsely from
their IANA timezone via a small lookup — enough to answer "is it hot / raining
where they are today", which is all an opener needs. An unknown timezone simply
yields no weather (the bundle still has day and headlines).

Hard rules honoured here:
  * fail-OPEN: any error / timeout returns ``None`` and the opener proceeds
    without weather — a flaky free API must never block a send.
  * bounded wait: a short timeout with one backed-off retry, never an unbounded
    hang on a scheduler tick.
  * ``follow_redirects=True`` per the httpx redirect lesson in CLAUDE.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from ...lib.logger import logger

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_REQUEST_TIMEOUT_S = 6.0
_MAX_ATTEMPTS = 2
_BASE_BACKOFF_S = 1.0

# Coarse IANA-timezone -> (latitude, longitude). Representative point per zone;
# precision beyond "this region" is unnecessary for a hot/cold/rain hook. Unknown
# zones return no weather. Extend freely — an unmapped zone degrades gracefully.
_TIMEZONE_COORDINATES: dict[str, tuple[float, float]] = {
    "Asia/Kolkata": (19.08, 72.88),       # Mumbai (representative for IN)
    "Asia/Calcutta": (19.08, 72.88),
    "Asia/Dubai": (25.20, 55.27),
    "Asia/Karachi": (24.86, 67.01),
    "Asia/Dhaka": (23.81, 90.41),
    "Asia/Singapore": (1.35, 103.82),
    "Asia/Tokyo": (35.68, 139.69),
    "Asia/Shanghai": (31.23, 121.47),
    "Asia/Manila": (14.60, 120.98),
    "Asia/Jakarta": (-6.21, 106.85),
    "Europe/London": (51.51, -0.13),
    "Europe/Paris": (48.85, 2.35),
    "Europe/Berlin": (52.52, 13.40),
    "Europe/Madrid": (40.42, -3.70),
    "Europe/Moscow": (55.76, 37.62),
    "America/New_York": (40.71, -74.01),
    "America/Chicago": (41.88, -87.63),
    "America/Denver": (39.74, -104.99),
    "America/Los_Angeles": (34.05, -118.24),
    "America/Toronto": (43.65, -79.38),
    "America/Sao_Paulo": (-23.55, -46.63),
    "America/Mexico_City": (19.43, -99.13),
    "Australia/Sydney": (-33.87, 151.21),
    "Africa/Lagos": (6.52, 3.38),
    "Africa/Johannesburg": (-26.20, 28.05),
    "Africa/Cairo": (30.04, 31.24),
}


@dataclass
class WeatherSummary:
    """A coarse, opener-ready description of today's weather."""

    condition: str        # clear | cloudy | foggy | rainy | snowy | stormy
    temperature_c: float
    temperature_band: str  # hot | warm | mild | cool | cold

    def describe(self) -> str:
        """One short phrase for the planner prompt, e.g. 'hot and clear (33C)'."""
        return f"{self.temperature_band} and {self.condition} ({round(self.temperature_c)}C)"


def _condition_from_wmo_code(code: int) -> str:
    """Map a WMO weather code to a coarse condition word."""
    if code == 0:
        return "clear"
    if code in (1, 2, 3):
        return "cloudy"
    if code in (45, 48):
        return "foggy"
    if code in (71, 73, 75, 77, 85, 86):
        return "snowy"
    if code in (95, 96, 99):
        return "stormy"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rainy"
    return "cloudy"


def _temperature_band(temp_c: float) -> str:
    if temp_c >= 32:
        return "hot"
    if temp_c >= 24:
        return "warm"
    if temp_c >= 15:
        return "mild"
    if temp_c >= 8:
        return "cool"
    return "cold"


def coordinates_for_timezone(timezone_name: str) -> tuple[float, float] | None:
    """Coarse coordinates for a timezone, or None if we cannot place it."""
    return _TIMEZONE_COORDINATES.get(timezone_name)


async def fetch_today_weather(timezone_name: str) -> WeatherSummary | None:
    """Today's coarse weather for the region of ``timezone_name``, or None.

    Returns None (never raises) when the timezone is unmappable, the API errors or
    times out, or the response is malformed — the caller proceeds without weather.
    """
    coords = coordinates_for_timezone(timezone_name)
    if coords is None:
        return None
    latitude, longitude = coords

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code",
        "timezone": "auto",
        "temperature_unit": "celsius",
    }

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_S, follow_redirects=True
            ) as client:
                resp = await client.get(_OPEN_METEO_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            current = data.get("current") or {}
            temp = current.get("temperature_2m")
            code = current.get("weather_code")
            if temp is None or code is None:
                return None
            temp_c = float(temp)
            return WeatherSummary(
                condition=_condition_from_wmo_code(int(code)),
                temperature_c=temp_c,
                temperature_band=_temperature_band(temp_c),
            )
        except Exception as exc:
            last_error = exc
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BASE_BACKOFF_S * attempt)

    # Fail-open: log once at debug; weather is a nice-to-have, not a dependency.
    logger.debug("icebreaker.weather: fetch failed, proceeding without weather", {
        "timezone": timezone_name,
        "error": str(last_error) if last_error else None,
    })
    return None
