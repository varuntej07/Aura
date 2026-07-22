"""
Tests for create_calendar_event attendee support in src/services/tool_executor.py.

The tool now accepts an ``attendees`` list so Buddy can invite guests in the same
call it creates the event. These tests pin the shape of what actually reaches the
Google Calendar API body (``{"email": ...}`` entries) and the defensive
normalization the executor applies to whatever the LLM emits.

GoogleCalendarConnector is imported at module scope in tool_executor, so it is
patched there, not at its source module.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.services.tool_executor import ToolExecutor, _normalize_attendee_emails

_GCC_PATH = "src.services.tool_executor.GoogleCalendarConnector"


def _fake_connector() -> tuple[MagicMock, dict]:
    """Return a connector whose insert() records the body it was handed."""
    captured: dict = {}
    connector = MagicMock()
    connector.get_status.return_value = {
        "enabled": True,
        "calendar_time_zone": "America/Los_Angeles",
    }

    def _insert(calendarId: str, body: dict):  # noqa: N803 (Google API kwarg name)
        captured["calendarId"] = calendarId
        captured["body"] = body
        execute = MagicMock()
        execute.execute.return_value = {"id": "evt-1", "htmlLink": "http://x", "status": "confirmed"}
        return execute

    connector.calendar_client.return_value.events.return_value.insert.side_effect = _insert
    return connector, captured


async def _create(inp: dict) -> tuple[dict, dict]:
    connector, captured = _fake_connector()
    with patch(_GCC_PATH, return_value=connector):
        result = await ToolExecutor("uid-1")._create_calendar_event(inp)
    return result, captured


@pytest.mark.asyncio
async def test_attendees_list_becomes_google_email_entries():
    result, captured = await _create({
        "title": "Lunch",
        "start_time": "2026-07-24T13:00:00-07:00",
        "attendees": ["sam@x.com", "kim@y.com"],
    })
    assert result["configured"] is True
    assert captured["body"]["attendees"] == [{"email": "sam@x.com"}, {"email": "kim@y.com"}]


@pytest.mark.asyncio
async def test_comma_joined_attendee_string_is_split():
    _, captured = await _create({
        "title": "Sync",
        "start_time": "2026-07-24T13:00:00-07:00",
        "attendees": "sam@x.com, kim@y.com",
    })
    assert captured["body"]["attendees"] == [{"email": "sam@x.com"}, {"email": "kim@y.com"}]


@pytest.mark.asyncio
async def test_no_attendees_leaves_body_without_the_key():
    _, captured = await _create({
        "title": "Solo focus",
        "start_time": "2026-07-24T13:00:00-07:00",
    })
    assert "attendees" not in captured["body"]


def test_normalize_drops_non_emails_and_dedupes_case_insensitively():
    assert _normalize_attendee_emails(["sam@x.com", "not-an-email", "SAM@x.com", " kim@y.com "]) == [
        "sam@x.com",
        "kim@y.com",
    ]
    assert _normalize_attendee_emails(None) == []
    assert _normalize_attendee_emails("") == []
    assert _normalize_attendee_emails(42) == []
