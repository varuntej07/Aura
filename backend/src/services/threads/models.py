"""Domain models for open-loop threads.

A *thread* is not a task to audit ("did you finish?"). It is a hole in what
Buddy knows about the user's life. The reflector later picks the most
interesting hole and asks about it like a curious friend; the answer enriches
the UserAura profile. Completion is never the question — understanding is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from . import fields as f


class ThreadSource(StrEnum):
    """Where the open loop came from."""

    REMINDER = "reminder"
    CHAT = "chat"
    VOICE = "voice"
    AURA_GAP = "aura_gap"   # a gap in the interest profile, not a user-mentioned loop


class ThreadStatus(StrEnum):
    """Lifecycle of a thread."""

    OPEN = "open"          # unfilled hole, eligible for a curiosity follow-up
    ENGAGED = "engaged"    # the user answered; the conversation is live
    RESOLVED = "resolved"  # the hole is filled; stop following up
    DORMANT = "dormant"    # ignored too many times; stop following up


def _coerce_datetime(value: Any) -> datetime | None:
    """Firestore returns timezone-aware datetimes; tolerate ISO strings too."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


@dataclass
class Thread:
    """One open-loop thread document."""

    thread_id: str
    trigger_text: str
    source: str
    category: str | None = None
    source_ref: str | None = None
    known_summary: str = ""
    unknown: list[str] = field(default_factory=list)
    status: str = ThreadStatus.OPEN
    created_at: datetime | None = None
    last_touched_at: datetime | None = None
    expected_resolution_at: datetime | None = None
    follow_ups_sent: int = 0
    last_follow_up_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise for Firestore. Every key goes through fields.py."""
        return {
            f.FIELD_THREAD_ID: self.thread_id,
            f.FIELD_TRIGGER_TEXT: self.trigger_text,
            f.FIELD_SOURCE: str(self.source),
            f.FIELD_CATEGORY: self.category,
            f.FIELD_SOURCE_REF: self.source_ref,
            f.FIELD_KNOWN_SUMMARY: self.known_summary,
            f.FIELD_UNKNOWN: list(self.unknown),
            f.FIELD_STATUS: str(self.status),
            f.FIELD_CREATED_AT: self.created_at,
            f.FIELD_LAST_TOUCHED_AT: self.last_touched_at,
            f.FIELD_EXPECTED_RESOLUTION_AT: self.expected_resolution_at,
            f.FIELD_FOLLOW_UPS_SENT: self.follow_ups_sent,
            f.FIELD_LAST_FOLLOW_UP_AT: self.last_follow_up_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thread:
        """Rebuild from a Firestore document. Every key goes through fields.py."""
        return cls(
            thread_id=str(data.get(f.FIELD_THREAD_ID, "")),
            trigger_text=str(data.get(f.FIELD_TRIGGER_TEXT, "")),
            source=str(data.get(f.FIELD_SOURCE, ThreadSource.CHAT)),
            category=data.get(f.FIELD_CATEGORY),
            source_ref=data.get(f.FIELD_SOURCE_REF),
            known_summary=str(data.get(f.FIELD_KNOWN_SUMMARY, "")),
            unknown=list(data.get(f.FIELD_UNKNOWN, []) or []),
            status=str(data.get(f.FIELD_STATUS, ThreadStatus.OPEN)),
            created_at=_coerce_datetime(data.get(f.FIELD_CREATED_AT)),
            last_touched_at=_coerce_datetime(data.get(f.FIELD_LAST_TOUCHED_AT)),
            expected_resolution_at=_coerce_datetime(data.get(f.FIELD_EXPECTED_RESOLUTION_AT)),
            follow_ups_sent=int(data.get(f.FIELD_FOLLOW_UPS_SENT, 0) or 0),
            last_follow_up_at=_coerce_datetime(data.get(f.FIELD_LAST_FOLLOW_UP_AT)),
        )
