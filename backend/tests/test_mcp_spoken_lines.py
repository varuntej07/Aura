"""Action Truth Contract (handlers/mcp.py).

Every voice write tool returns a ready-to-speak `say` line with its result, so
Buddy echoes the tool's truth instead of composing its own success claim. A
not-linked integration gets a warm "connect it" line instead of a bare
failure, and error/timeout results never carry a success line.
"""

from __future__ import annotations

from src.handlers.mcp import _with_spoken_line


def test_success_result_carries_spoken_line():
    result = _with_spoken_line({"event_id": "e1"}, ok="Done, added it.")
    assert result["say"] == "Done, added it."
    assert result["event_id"] == "e1"


def test_error_result_gets_no_success_line():
    result = _with_spoken_line(
        {"error": True, "user_message": "That took too long."},
        ok="Done, added it.",
    )
    assert "say" not in result


def test_not_linked_result_explains_instead_of_refusing():
    result = _with_spoken_line(
        {"configured": False, "message": "Google Calendar is not configured."},
        ok="Done, added it.",
        not_linked="Your Google Calendar isn't linked yet.",
    )
    assert result["say"] == "Your Google Calendar isn't linked yet."


def test_not_linked_without_mapping_falls_back_to_ok_line():
    # Tools without an integration (reminders) never pass not_linked; a result
    # that happens to carry configured=False still gets the ok line rather than
    # crashing or going silent.
    result = _with_spoken_line({"configured": False}, ok="Done.")
    assert result["say"] == "Done."


def test_original_result_fields_survive_untouched():
    original = {"event_id": "e1", "html_link": "https://cal", "status": "confirmed"}
    result = _with_spoken_line(dict(original), ok="Done.")
    for key, value in original.items():
        assert result[key] == value
