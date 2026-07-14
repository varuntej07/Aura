"""Pure-function coverage for which open loop the reflector chooses to ask about.

select_thread_to_follow_up does no I/O, so the policy (eligibility + ordering)
is pinned here without touching Firestore or an LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.services.threads.models import Thread, ThreadSource, ThreadStatus
from src.services.threads.thread_reflector import (
    FOLLOW_UP_COOLDOWN,
    MAX_FOLLOW_UPS_PER_THREAD,
    MIN_THREAD_AGE_BEFORE_FOLLOW_UP,
    REMINDER_SETTLE_BUFFER,
    select_thread_to_follow_up,
)

NOW = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)


def _thread(
    thread_id: str,
    *,
    status: ThreadStatus = ThreadStatus.OPEN,
    created_ago: timedelta = timedelta(hours=5),
    touched_ago: timedelta = timedelta(hours=5),
    follow_ups_sent: int = 0,
    last_follow_up_ago: timedelta | None = None,
    expected_resolution_at: datetime | None = None,
) -> Thread:
    return Thread(
        thread_id=thread_id,
        trigger_text=f"loop {thread_id}",
        source=ThreadSource.REMINDER,
        status=status,
        created_at=NOW - created_ago,
        last_touched_at=NOW - touched_ago,
        follow_ups_sent=follow_ups_sent,
        last_follow_up_at=None if last_follow_up_ago is None else NOW - last_follow_up_ago,
        expected_resolution_at=expected_resolution_at,
    )


def test_empty_returns_none():
    assert select_thread_to_follow_up([], NOW) is None


def test_open_eligible_thread_is_chosen():
    chosen = select_thread_to_follow_up([_thread("a")], NOW)
    assert chosen is not None and chosen.thread_id == "a"


def test_non_open_status_is_excluded():
    for status in (ThreadStatus.RESOLVED, ThreadStatus.ENGAGED, ThreadStatus.DORMANT):
        assert select_thread_to_follow_up([_thread("a", status=status)], NOW) is None


def test_too_new_thread_is_excluded():
    fresh = _thread("a", created_ago=MIN_THREAD_AGE_BEFORE_FOLLOW_UP - timedelta(minutes=1))
    assert select_thread_to_follow_up([fresh], NOW) is None


def test_thread_at_followup_cap_is_excluded():
    maxed = _thread("a", follow_ups_sent=MAX_FOLLOW_UPS_PER_THREAD)
    assert select_thread_to_follow_up([maxed], NOW) is None


def test_thread_in_cooldown_is_excluded():
    recent = _thread("a", follow_ups_sent=1, last_follow_up_ago=FOLLOW_UP_COOLDOWN - timedelta(hours=1))
    assert select_thread_to_follow_up([recent], NOW) is None


def test_thread_past_cooldown_is_eligible():
    ready = _thread("a", follow_ups_sent=1, last_follow_up_ago=FOLLOW_UP_COOLDOWN + timedelta(hours=1))
    chosen = select_thread_to_follow_up([ready], NOW)
    assert chosen is not None and chosen.thread_id == "a"


def test_reminder_still_within_settle_buffer_is_excluded():
    # Due 30 min ago -> still inside the 2h settle buffer.
    just_fired = _thread("a", expected_resolution_at=NOW - timedelta(minutes=30))
    assert select_thread_to_follow_up([just_fired], NOW) is None


def test_reminder_past_settle_buffer_is_eligible():
    settled = _thread("a", expected_resolution_at=NOW - (REMINDER_SETTLE_BUFFER + timedelta(minutes=1)))
    chosen = select_thread_to_follow_up([settled], NOW)
    assert chosen is not None and chosen.thread_id == "a"


def test_reminder_not_yet_due_is_excluded():
    # A reminder set far in the future must not be followed up on before it's due,
    # even though the thread itself is already past MIN_THREAD_AGE_BEFORE_FOLLOW_UP.
    not_due = _thread("a", expected_resolution_at=NOW + timedelta(hours=1))
    assert select_thread_to_follow_up([not_due], NOW) is None


def test_prefers_fewest_follow_ups_then_most_recent_mention():
    never_asked_old = _thread("old", follow_ups_sent=0, touched_ago=timedelta(hours=10))
    never_asked_fresh = _thread("fresh", follow_ups_sent=0, touched_ago=timedelta(hours=2))
    asked_once_fresh = _thread(
        "asked", follow_ups_sent=1, touched_ago=timedelta(hours=1),
        last_follow_up_ago=FOLLOW_UP_COOLDOWN + timedelta(hours=1),
    )
    chosen = select_thread_to_follow_up(
        [asked_once_fresh, never_asked_old, never_asked_fresh], NOW
    )
    # Fewest follow-ups wins first (0 beats 1); among those, the most recently
    # mentioned loop is the most natural to ask about.
    assert chosen is not None and chosen.thread_id == "fresh"
