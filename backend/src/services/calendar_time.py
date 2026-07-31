"""Strict server-side interpretation of natural-language calendar event times."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .reminder_time import ParsedReminderTime, parse_reminder_when
from .timezone_utils import resolve_timezone

_DEFAULT_DURATION = timedelta(minutes=60)
_DURATION_EXPRESSION = re.compile(
    r"^(?P<start>.+?)\s+for\s+(?P<duration>.+)$",
    re.IGNORECASE,
)
_ENDING_EXPRESSION = re.compile(
    r"^(?P<date>.+?)\s+from\s+(?P<start>.+?)\s+to\s+(?P<end>.+)$",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"^(?P<number>[a-z\d -]+)\s+(?P<unit>minutes?|hours?)$",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
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


@dataclass(frozen=True, slots=True)
class ParsedCalendarTime:
    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime
    timezone: str


def _parse_number(value: str) -> int | None:
    raw = value.lower().strip()
    if raw.startswith("-"):
        return None
    normalized = raw.replace("-", " ")
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


def _parse_duration(value: str) -> timedelta:
    normalized = " ".join(value.strip().split())
    if normalized.lower() == "half an hour":
        return timedelta(minutes=30)
    match = _DURATION.fullmatch(normalized)
    if match is None:
        raise ValueError(
            "I couldn't understand that duration. Try 'for 45 minutes' or 'for an hour'."
        )
    amount = _parse_number(match.group("number"))
    if amount is None:
        raise ValueError(
            "The event duration must be greater than zero, like 'for 45 minutes'."
        )
    if match.group("unit").lower().startswith("hour"):
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _from_start_and_delta(
    start: ParsedReminderTime,
    duration: timedelta,
) -> ParsedCalendarTime:
    end_utc = start.utc + duration
    zone = resolve_timezone(start.timezone).zone
    return ParsedCalendarTime(
        start_utc=start.utc,
        end_utc=end_utc,
        start_local=start.local,
        end_local=end_utc.astimezone(zone),
        timezone=start.timezone,
    )


def parse_calendar_when(when: str, timezone_name: str) -> ParsedCalendarTime:
    """Resolve a complete event time expression in the user's IANA timezone."""
    value = " ".join(when.strip().split())
    if not value:
        raise ValueError(
            "Tell me when the event starts, like 'tomorrow at 9 AM'."
        )

    ending_match = _ENDING_EXPRESSION.fullmatch(value)
    if ending_match:
        date_text = ending_match.group("date")
        start = parse_reminder_when(
            f"{date_text} at {ending_match.group('start')}",
            timezone_name,
        )
        end = parse_reminder_when(
            f"{date_text} at {ending_match.group('end')}",
            timezone_name,
        )
        if end.utc <= start.utc:
            raise ValueError(
                "The event ending time must be after its start time. "
                "Say both times again with AM or PM."
            )
        return ParsedCalendarTime(
            start_utc=start.utc,
            end_utc=end.utc,
            start_local=start.local,
            end_local=end.local,
            timezone=start.timezone,
        )

    if re.search(r"\b(?:from|to)\b", value, re.IGNORECASE):
        raise ValueError(
            "I couldn't understand the complete start and ending time. "
            "Try 'Friday from 2 PM to 3:30 PM'."
        )

    duration_match = _DURATION_EXPRESSION.fullmatch(value)
    if duration_match:
        start = parse_reminder_when(duration_match.group("start"), timezone_name)
        duration = _parse_duration(duration_match.group("duration"))
        return _from_start_and_delta(start, duration)

    if re.search(r"\bfor\b", value, re.IGNORECASE):
        raise ValueError(
            "I couldn't understand the complete duration. "
            "Try 'tomorrow at 9 AM for 45 minutes'."
        )

    start = parse_reminder_when(value, timezone_name)
    return _from_start_and_delta(start, _DEFAULT_DURATION)
