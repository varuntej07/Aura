"""The proactive-notification "why I reached out" reason.

Contract introduced 2026-06-22: a proactive push (news/signal, icebreaker, thread)
carries a short Buddy-facing reason that the chat handler injects into the system
prompt on the FIRST reply after a tap, so Buddy stays oriented on the opener it
sent instead of disowning it. Two things are guarded here:

1. The chat handler injects the reason as an UNCACHED trailing block (never in the
   cached prefix) and omits it entirely on a normal turn.
2. The thread reason names the specific thing and reads as curiosity, never a task
   audit — the exact failure ("you always forgetting / staying on top of it") that
   prompted the fix.
"""

from __future__ import annotations

from src.handlers.chat import _build_system_blocks
from src.services.threads.models import Thread, ThreadSource
from src.services.threads.thread_reflector import (
    THREAD_REASON_MAX_CHARS,
    _build_thread_reason,
)


def _texts(blocks: list[dict]) -> list[str]:
    return [b["text"] for b in blocks]


def test_system_blocks_inject_reason_as_uncached_trailing_block():
    reason = "Earlier they mentioned calling the bank about the lease deposit."
    blocks = _build_system_blocks("BASE", "AURA", "2026-06-22 10:00", reason)
    texts = _texts(blocks)

    reason_blocks = [t for t in texts if "WHY YOU REACHED OUT" in t]
    assert len(reason_blocks) == 1
    assert reason in reason_blocks[0]
    # It must be LAST and uncached: the cached prefix (base + aura) is stable, but
    # this note is volatile (first-turn only) and must not poison the cache.
    assert blocks[-1]["text"] == reason_blocks[0]
    assert "cache_control" not in blocks[-1]


def test_system_blocks_omit_reason_when_absent():
    blocks = _build_system_blocks("BASE", "AURA", "2026-06-22 10:00", "")
    assert all("WHY YOU REACHED OUT" not in t for t in _texts(blocks))
    # The cached prefix is untouched when there is no reason.
    assert blocks[0]["text"] == "BASE"
    assert blocks[0]["cache_control"]["ttl"] == "1h"


def test_system_blocks_reason_defaults_off_for_normal_turns():
    # A normal chat turn never passes a reason; the param defaults empty.
    blocks = _build_system_blocks("BASE", "", "2026-06-22 10:00")
    assert all("WHY YOU REACHED OUT" not in t for t in _texts(blocks))


def test_thread_reason_names_the_trigger_and_stays_curious():
    thread = Thread(
        thread_id="t1",
        trigger_text="call the bank about the lease deposit",
        source=ThreadSource.REMINDER,
        known_summary="They set this as a reminder two days ago.",
    )
    reason = _build_thread_reason(thread)

    # Names the specific thing, so a full-chat open is oriented, not blind.
    assert "call the bank about the lease deposit" in reason
    assert "They set this as a reminder two days ago." in reason
    # Reads as curiosity, never an accountability check-in.
    lowered = reason.lower()
    for banned in ("did you finish", "forgetting", "stay on top", "completed"):
        assert banned not in lowered
    assert "curious" in lowered


def test_thread_reason_handles_empty_thread_and_caps_length():
    # No trigger / no summary still yields a usable, curious note (never empty).
    bare = _build_thread_reason(
        Thread(thread_id="t2", trigger_text="", source=ThreadSource.AURA_GAP)
    )
    assert bare.strip()
    assert "curious" in bare.lower()

    # A pathologically long trigger is capped for FCM payload + prompt hygiene.
    huge = _build_thread_reason(
        Thread(thread_id="t3", trigger_text="x" * 5000, source=ThreadSource.CHAT)
    )
    assert len(huge) <= THREAD_REASON_MAX_CHARS
