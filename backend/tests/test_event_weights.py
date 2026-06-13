"""
Opening a notification trains the user vector only MILDLY now (Layer 5).

A mere tap means the copy was tempting, not that the content was endorsed. The
strong positive signal moves to what the user does next — reading the article
(content_opened) or replying (the chat path). This pins that contract so a future
edit can't quietly restore the old over-weighted 1.0 open.
"""

from __future__ import annotations

from src.services.signal_engine.event_ingester import EVENT_WEIGHTS


def test_notification_open_is_a_mild_signal_not_full_endorsement():
    # Opening is a small nudge, strictly weaker than actually reading or liking.
    assert EVENT_WEIGHTS["notification_opened"] == 0.3
    assert EVENT_WEIGHTS["notification_opened"] < EVENT_WEIGHTS["content_opened"]
    assert EVENT_WEIGHTS["content_opened"] < EVENT_WEIGHTS["content_view_long"]
    assert EVENT_WEIGHTS["content_view_long"] < EVENT_WEIGHTS["content_liked"]


def test_content_opened_is_a_known_signal_event():
    # The read-tap event must be accepted by the /events endpoint (its known set is
    # derived from EVENT_WEIGHTS) and carry a positive weight toward the content.
    from src.handlers.signal_events import KNOWN_EVENT_TYPES

    assert "content_opened" in KNOWN_EVENT_TYPES
    assert EVENT_WEIGHTS["content_opened"] > 0
