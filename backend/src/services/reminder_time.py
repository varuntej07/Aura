"""Strict server-side interpretation of natural-language reminder times."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from .timezone_utils import TimezoneResolutionError, resolve_timezone

_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_NUMERIC_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b")
_AMBIGUOUS_NEXT_WEEKDAY = re.compile(
    r"\bnext\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_HOUR_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUMBER_WORDS = {
    **_HOUR_WORDS,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_CLOCK = (
    r"(?:noon|midnight|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"\d{1,2})(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"(?:1[3-9]|2[0-3]):[0-5]\d)"
)
_WEEKDAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_MONTH = (
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)"
)
_DATE = (
    rf"(?:today|tomorrow|tonight|(?:this\s+)?{_WEEKDAY}|"
    rf"(?:{_WEEKDAY},?\s+)?{_MONTH}\s+\d{{1,2}}(?:,?\s+\d{{4}})?)"
)
_DATE_THEN_TIME = re.compile(
    rf"^(?P<date>{_DATE})\s+at\s+(?P<clock>{_CLOCK})$",
    re.IGNORECASE,
)
_TIME_THEN_DATE = re.compile(
    rf"^at\s+(?P<clock>{_CLOCK})\s+(?P<date>{_DATE})$",
    re.IGNORECASE,
)
_AMBIGUOUS_CLOCK = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"(?:0?[1-9]|1[0-2]))(?::[0-5]\d)?"
)
_AMBIGUOUS_DATE_THEN_TIME = re.compile(
    rf"^{_DATE}\s+at\s+{_AMBIGUOUS_CLOCK}$",
    re.IGNORECASE,
)
_AMBIGUOUS_TIME_THEN_DATE = re.compile(
    rf"^at\s+{_AMBIGUOUS_CLOCK}\s+{_DATE}$",
    re.IGNORECASE,
)
_RELATIVE_DURATION = re.compile(
    r"^(?:in|after)\s+(?P<number>[a-z\d -]+)\s+(?P<unit>minutes?|hours?)$",
    re.IGNORECASE,
)
_MONTH_DATE = re.compile(
    rf"^(?:(?P<weekday>{_WEEKDAY}),?\s+)?"
    rf"(?P<month>{_MONTH})\s+(?P<day>\d{{1,2}})(?:,?\s+(?P<year>\d{{4}}))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedReminderTime:
    utc: datetime
    local: datetime
    timezone: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_number(value: str) -> int | None:
    normalized = value.lower().replace("-", " ").strip()
    if normalized.isdigit():
        number = int(normalized)
        return number if number > 0 else None
    if normalized in {"a", "an"}:
        return 1
    tokens = normalized.split()
    if len(tokens) == 1:
        return _NUMBER_WORDS.get(tokens[0])
    if (
        len(tokens) == 2
        and _NUMBER_WORDS.get(tokens[0], 0) >= 20
        and 1 <= _NUMBER_WORDS.get(tokens[1], 0) <= 9
    ):
        return _NUMBER_WORDS[tokens[0]] + _NUMBER_WORDS[tokens[1]]
    return None


def _parse_clock(value: str) -> time:
    normalized = value.lower().replace(".", "").strip()
    if normalized == "noon":
        return time(12)
    if normalized == "midnight":
        return time(0)
    if re.fullmatch(r"(?:1[3-9]|2[0-3]):[0-5]\d", normalized):
        return time.fromisoformat(normalized)

    match = re.fullmatch(
        r"(?P<hour>[a-z]+|\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<period>am|pm)",
        normalized,
    )
    if match is None:
        raise ValueError(
            "That time needs AM or PM. Try something like 'tomorrow at 9 AM'."
        )
    hour_text = match.group("hour")
    hour = _HOUR_WORDS.get(hour_text, int(hour_text) if hour_text.isdigit() else 0)
    if not 1 <= hour <= 12:
        raise ValueError("Use an hour from 1 through 12 with AM or PM.")
    minute = int(match.group("minute") or 0)
    if match.group("period") == "am":
        hour %= 12
    elif hour != 12:
        hour += 12
    return time(hour, minute)


def _parse_date(value: str, local_today: date) -> date:
    normalized = value.lower().strip()
    if normalized in {"today", "tonight"}:
        return local_today
    if normalized == "tomorrow":
        return local_today + timedelta(days=1)
    weekday_text = normalized.removeprefix("this ")
    if weekday_text in _WEEKDAYS:
        days_ahead = (_WEEKDAYS[weekday_text] - local_today.weekday()) % 7
        return local_today + timedelta(days=days_ahead)

    match = _MONTH_DATE.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "I couldn't understand that date. Try one like 'Friday, August 7 at 3 PM'."
        )
    year = int(match.group("year") or local_today.year)
    try:
        parsed = date(year, _MONTHS[match.group("month")], int(match.group("day")))
    except ValueError as exc:
        raise ValueError(
            "That calendar date isn't valid. Check the month and day, then try again."
        ) from exc
    stated_weekday = match.group("weekday")
    if stated_weekday and parsed.weekday() != _WEEKDAYS[stated_weekday]:
        raise ValueError(
            "That weekday doesn't match the calendar date. Check the date, then try again."
        )
    return parsed


def _valid_local_candidates(local_wall_time: datetime, timezone_name: str) -> list[datetime]:
    zone = resolve_timezone(timezone_name).zone
    candidates: list[datetime] = []
    seen_utc: set[datetime] = set()
    for fold in (0, 1):
        candidate = local_wall_time.replace(tzinfo=zone, fold=fold)
        utc_candidate = candidate.astimezone(UTC)
        round_trip = utc_candidate.astimezone(zone).replace(tzinfo=None)
        if round_trip == local_wall_time and utc_candidate not in seen_utc:
            candidates.append(candidate)
            seen_utc.add(utc_candidate)
    return candidates


def parse_reminder_when(when: str, timezone_name: str) -> ParsedReminderTime:
    """Resolve one complete natural-language expression against the user's local clock."""
    value = " ".join(when.strip().split())
    if not value:
        raise ValueError("Tell me when to remind you, like 'tomorrow at 9 AM'.")
    if _ISO_DATETIME.match(value):
        raise ValueError(
            "Use a natural time like 'tomorrow at 9 AM', not a formatted timestamp."
        )
    if _NUMERIC_DATE.search(value):
        raise ValueError(
            "That numeric date could mean two different dates. Say the month by name, "
            "like 'August 9 at 3 PM'."
        )
    if _AMBIGUOUS_NEXT_WEEKDAY.search(value):
        raise ValueError(
            "That weekday could mean two dates. Say the calendar date and time you mean."
        )

    try:
        resolved_timezone = resolve_timezone(timezone_name)
    except TimezoneResolutionError as exc:
        raise ValueError(
            "I need your current timezone before I can set this safely. "
            "Refresh your device timezone, then try again."
        ) from exc

    now_utc = _utc_now()
    local_now = now_utc.astimezone(resolved_timezone.zone)
    relative_match = _RELATIVE_DURATION.fullmatch(value)
    if relative_match:
        amount = _parse_number(relative_match.group("number"))
        if amount is None:
            raise ValueError(
                "I couldn't understand that duration. Try something like 'in 45 minutes'."
            )
        unit = (
            "hours"
            if relative_match.group("unit").lower().startswith("hour")
            else "minutes"
        )
        delta = timedelta(**{unit: amount})
        trigger_at = now_utc + delta
        return ParsedReminderTime(
            trigger_at,
            trigger_at.astimezone(resolved_timezone.zone),
            resolved_timezone.canonical_name,
        )

    wall_match = _DATE_THEN_TIME.fullmatch(value) or _TIME_THEN_DATE.fullmatch(value)
    if wall_match is None:
        if (
            _AMBIGUOUS_DATE_THEN_TIME.fullmatch(value)
            or _AMBIGUOUS_TIME_THEN_DATE.fullmatch(value)
        ):
            raise ValueError(
                "That time needs AM or PM. Try something like 'tomorrow at 9 AM'."
            )
        if re.fullmatch(_DATE, value, re.IGNORECASE):
            raise ValueError(
                "I need an exact time too. Try something like 'tomorrow at 9 AM'."
            )
        raise ValueError(
            "I couldn't understand the complete time. Try a specific expression, "
            "like 'Friday, August 7 at 3 PM'."
        )

    local_date = _parse_date(wall_match.group("date"), local_now.date())
    local_clock = _parse_clock(wall_match.group("clock"))
    local_wall_time = datetime.combine(local_date, local_clock)
    candidates = _valid_local_candidates(local_wall_time, resolved_timezone.canonical_name)
    if not candidates:
        raise ValueError(
            "That local time doesn't exist because the clocks change then. "
            "Choose a valid time outside the daylight-saving jump."
        )
    if len(candidates) > 1:
        raise ValueError(
            "That local time happens twice because the clocks change then. "
            "Choose a different time outside the repeated hour."
        )

    local_time = candidates[0]
    trigger_at = local_time.astimezone(UTC)
    if trigger_at <= now_utc:
        raise ValueError("That time has already passed. Tell me a future date and time.")
    return ParsedReminderTime(
        trigger_at,
        local_time,
        resolved_timezone.canonical_name,
    )
