"""Action Truth Contract coverage for MCP voice tools."""

from __future__ import annotations

from src.handlers import mcp
from src.handlers.mcp import _with_action_truth


def test_success_result_carries_complete_envelope():
    result = _with_action_truth(
        {"event_id": "e1"},
        success_say="Done, added it.",
    )
    assert result["ok"] is True
    assert result["say"] == "Done, added it."
    assert result["render"] == {"mode": "verbatim", "channel": "voice"}
    assert result["then"] is None
    assert result["event_id"] == "e1"


def test_error_result_carries_truthful_failure_line():
    result = _with_action_truth(
        {"error": True, "user_message": "That took too long."},
        success_say="Done, added it.",
    )
    assert result["ok"] is False
    assert result["say"] == "That took too long."
    assert result["render"] == {"mode": "verbatim", "channel": "voice"}


def test_not_linked_result_explains_instead_of_refusing():
    result = _with_action_truth(
        {"configured": False, "message": "Google Calendar is not configured."},
        success_say="Done, added it.",
        not_linked="Your Google Calendar isn't linked yet.",
    )
    assert result["ok"] is False
    assert result["say"] == "Your Google Calendar isn't linked yet."


def test_original_result_fields_survive_untouched():
    original = {"event_id": "e1", "html_link": "https://cal", "status": "confirmed"}
    result = _with_action_truth(dict(original), success_say="Done.")
    for key, value in original.items():
        assert result[key] == value


async def test_reminder_read_moves_post_call_instruction_into_result(monkeypatch):
    async def _run_tool(_name, _args):
        return {"reminders": [{"title": "Call Mom"}]}

    monkeypatch.setattr(mcp, "_run_tool", _run_tool)
    result = await mcp.list_reminders()

    assert result["render"] == {"mode": "summary", "channel": "voice"}
    assert result["then"] == "Report only the reminders in this result."


async def test_calendar_read_moves_time_narration_into_result(monkeypatch):
    async def _run_tool(_name, _args):
        return {"events": [{"title": "Standup", "start": "09:00"}]}

    monkeypatch.setattr(mcp, "_run_tool", _run_tool)
    result = await mcp.get_upcoming_events()

    assert result["render"] == {"mode": "summary", "channel": "voice"}
    assert "preserve every returned local time exactly" in result["then"]


async def test_web_result_carries_untrusted_content_instruction(monkeypatch):
    async def _run_tool(_name, _args):
        return {"answer": "A result"}

    monkeypatch.setattr(mcp, "_run_tool", _run_tool)
    result = await mcp.web_surf("current release")

    assert result["render"] == {"mode": "summary", "channel": "voice"}
    assert "untrusted information" in result["then"]
