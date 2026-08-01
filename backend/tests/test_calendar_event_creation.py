from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import llm as lk_llm
from livekit.agents.llm._provider_format.openai import to_fnc_ctx
from mcp.server.fastmcp.exceptions import ToolError

import src.services.reminder_time as reminder_time
import src.services.tool_executor as tool_executor_module
from src.agent.voice.capabilities import VOICE_TOOL_REGISTRY
from src.shared.tools import tool_definition
from src.agent.voice.pipelines import AuraMCPServerHTTP
from src.handlers import mcp
from src.services.calendar_time import parse_calendar_when
from src.services.openai_chat_fallback import _anthropic_tools_to_openai
from src.services.tool_executor import ToolExecutor, _normalize_attendee_emails
from src.shared.tools import (
    CREATE_CALENDAR_EVENT_TOOL_DEFINITION,
    claude_tool_definitions,
    openai_function_definition,
)

_NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
_VALID_INPUT = {
    "title": "Planning",
    "when": "tomorrow at 9 AM",
    "description": "",
    "location": "",
    "attendees": [],
}
_ACCEPTED_CASES = [
    (
        "tomorrow at nine AM",
        "2026-07-29T09:00:00-07:00",
        "2026-07-29T10:00:00-07:00",
        "2026-07-29T16:00:00+00:00",
        "2026-07-29T17:00:00+00:00",
    ),
    (
        "tomorrow at 9 AM for 45 minutes",
        "2026-07-29T09:00:00-07:00",
        "2026-07-29T09:45:00-07:00",
        "2026-07-29T16:00:00+00:00",
        "2026-07-29T16:45:00+00:00",
    ),
    (
        "Friday from 2 PM to 3:30 PM",
        "2026-07-31T14:00:00-07:00",
        "2026-07-31T15:30:00-07:00",
        "2026-07-31T21:00:00+00:00",
        "2026-07-31T22:30:00+00:00",
    ),
    (
        "at noon tomorrow for an hour",
        "2026-07-29T12:00:00-07:00",
        "2026-07-29T13:00:00-07:00",
        "2026-07-29T19:00:00+00:00",
        "2026-07-29T20:00:00+00:00",
    ),
]


def _freeze(monkeypatch, instant: datetime) -> None:
    monkeypatch.setattr(reminder_time, "_utc_now", lambda: instant)


def _fake_connector() -> tuple[MagicMock, list[dict]]:
    inserts: list[dict] = []
    connector = MagicMock()
    connector.get_status.return_value = {
        "enabled": True,
        "calendar_time_zone": "Europe/London",
    }
    events = connector.calendar_client.return_value.events.return_value

    def _insert(**kwargs):
        inserts.append(kwargs)
        request = MagicMock()
        request.execute.return_value = {
            "id": kwargs["body"].get("id", "evt-1"),
            "htmlLink": "https://calendar.test/event",
            "status": "confirmed",
        }
        return request

    events.insert.side_effect = _insert
    return connector, inserts


async def _execute(
    monkeypatch,
    input_data: dict,
    *,
    now: datetime = _NOW,
    session_id: str = "voice-session-1",
) -> tuple[dict, MagicMock, list[dict]]:
    connector, inserts = _fake_connector()
    monkeypatch.setattr(
        tool_executor_module,
        "GoogleCalendarConnector",
        lambda _uid: connector,
    )
    monkeypatch.setattr(
        tool_executor_module,
        "_get_user_timezone",
        AsyncMock(return_value="America/Los_Angeles"),
    )
    _freeze(monkeypatch, now)
    result = await ToolExecutor(
        "uid-1",
        created_via="voice",
        session_id=session_id,
    ).execute("create_calendar_event", input_data)
    return result, connector, inserts


def test_canonical_calendar_schema_is_exact_and_strict():
    expected = {
        "name": "create_calendar_event",
        "description": (
            "Create a Google Calendar event only when the user asks to create or schedule "
            "a calendar event. Do not use for calendar reads, availability questions, or "
            "reminders, and never for a change to an event that already exists "
            "(update_calendar_event owns that). Never invent a date, a time, or an "
            "attendee: if the user did not say it, ask one short question first. Pass the "
            "time as natural language; the server resolves it against their local clock."
        ),
        "strict": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name for the calendar event.",
                },
                "when": {
                    "type": "string",
                    "description": (
                        "The user's natural-language start time and any duration or ending "
                        "time they supplied, such as 'tomorrow at 9 AM for 45 minutes' or "
                        "'Friday from 2 PM to 3:30 PM'. If the user supplied no duration or "
                        "ending time, omit one from this string; the server uses a 60-minute "
                        "duration. Never use ISO 8601."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Event notes, or an empty string when omitted.",
                },
                "location": {
                    "type": "string",
                    "description": "Event location, or an empty string when omitted.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Guest email addresses to invite, or an empty array when omitted."
                    ),
                },
            },
            "required": [
                "title",
                "when",
                "description",
                "location",
                "attendees",
            ],
            "additionalProperties": False,
        },
    }
    assert CREATE_CALENDAR_EVENT_TOOL_DEFINITION == expected
    anthropic = next(
        tool
        for tool in claude_tool_definitions()
        if tool["name"] == "create_calendar_event"
    )
    assert anthropic == {
        "name": expected["name"],
        "description": expected["description"],
        "input_schema": expected["inputSchema"],
    }
    assert _anthropic_tools_to_openai([anthropic]) == [
        {
            "type": "function",
            "function": openai_function_definition("create_calendar_event"),
        }
    ]
    assert tuple(inspect.signature(mcp.create_calendar_event).parameters) == (
        "title",
        "when",
        "description",
        "location",
        "attendees",
    )


async def test_fastmcp_discovery_matches_canonical_calendar_contract():
    tools = await mcp.mcp_server.list_tools()
    tool = next(tool for tool in tools if tool.name == "create_calendar_event")
    assert tool.description == CREATE_CALENDAR_EVENT_TOOL_DEFINITION["description"]
    assert tool.inputSchema == CREATE_CALENDAR_EVENT_TOOL_DEFINITION["inputSchema"]


async def test_actual_livekit_openai_serialization_matches_calendar_contract():
    tools = await mcp.mcp_server.list_tools()
    fastmcp_tool = next(
        tool for tool in tools if tool.name == "create_calendar_event"
    )
    server = AuraMCPServerHTTP(
        url="http://example.test/mcp/",
        transport_type="streamable_http",
    )
    livekit_tool = server._make_function_tool(
        fastmcp_tool.name,
        fastmcp_tool.description,
        fastmcp_tool.inputSchema,
        fastmcp_tool.meta,
    )

    assert to_fnc_ctx(lk_llm.ToolContext([livekit_tool])) == [
        {
            "type": "function",
            "function": openai_function_definition("create_calendar_event"),
        }
    ]


async def test_real_fastmcp_rejects_legacy_and_unknown_fields_before_execution(
    monkeypatch,
):
    run_tool = AsyncMock()
    monkeypatch.setattr(mcp, "_run_tool", run_tool)
    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        await mcp.mcp_server.call_tool(
            "create_calendar_event",
            {
                **_VALID_INPUT,
                "start_time": "2026-07-29T16:00:00Z",
                "end_time": "2026-07-29T17:00:00Z",
                "unexpected": True,
            },
        )
    run_tool.assert_not_awaited()


@pytest.mark.parametrize(
    (
        "when",
        "expected_start_local",
        "expected_end_local",
        "expected_start_utc",
        "expected_end_utc",
    ),
    _ACCEPTED_CASES,
)
def test_calendar_voice_expression_matrix(
    monkeypatch,
    when: str,
    expected_start_local: str,
    expected_end_local: str,
    expected_start_utc: str,
    expected_end_utc: str,
):
    _freeze(monkeypatch, _NOW)
    parsed = parse_calendar_when(when, "America/Los_Angeles")
    assert parsed.start_local.isoformat() == expected_start_local
    assert parsed.end_local.isoformat() == expected_end_local
    assert parsed.start_utc.isoformat() == expected_start_utc
    assert parsed.end_utc.isoformat() == expected_end_utc
    assert parsed.timezone == "America/Los_Angeles"


def test_missing_duration_defaults_to_exactly_sixty_minutes(monkeypatch):
    _freeze(monkeypatch, _NOW)
    parsed = parse_calendar_when(
        "tomorrow at nine AM",
        "America/Los_Angeles",
    )
    assert (parsed.end_utc - parsed.start_utc).total_seconds() == 3600


@pytest.mark.parametrize(
    ("when", "now", "expected"),
    [
        ("tomorrow at 9", _NOW, "AM or PM"),
        ("08/09 at 3 PM", _NOW, "numeric date"),
        ("February 30, 2027 at 3 PM", _NOW, "isn't valid"),
        ("today at 9 AM", _NOW, "already passed"),
        ("tomorrow at 9 AM for 0 minutes", _NOW, "greater than zero"),
        ("tomorrow at 9 AM for -5 minutes", _NOW, "greater than zero"),
        ("tomorrow at 9 AM for a while", _NOW, "duration"),
        (
            "March 8, 2026 at 2:30 AM",
            datetime(2026, 3, 1, 12, tzinfo=UTC),
            "doesn't exist",
        ),
        (
            "November 1, 2026 at 1:30 AM",
            datetime(2026, 10, 1, 12, tzinfo=UTC),
            "happens twice",
        ),
        ("Friday from 3 PM to 2 PM", _NOW, "must be after"),
        ("Friday from 2 PM", _NOW, "complete start and ending"),
        (
            "tomorrow at 9 AM and Friday at 3 PM",
            _NOW,
            "complete time",
        ),
    ],
)
async def test_every_time_rejection_performs_zero_google_writes(
    monkeypatch,
    when: str,
    now: datetime,
    expected: str,
):
    result, _connector, inserts = await _execute(
        monkeypatch,
        {**_VALID_INPUT, "when": when},
        now=now,
    )
    assert result["ok"] is False
    assert result["code"] == "validation_error"
    assert expected in result["user_message"]
    assert inserts == []


@pytest.mark.parametrize("field", ["start_time", "end_time", "unexpected"])
async def test_tool_executor_rejects_every_unknown_field_without_google_writes(
    monkeypatch,
    field: str,
):
    result, _connector, inserts = await _execute(
        monkeypatch,
        {**_VALID_INPUT, field: "legacy"},
    )
    assert result["ok"] is False
    assert result["code"] == "validation_error"
    assert result["user_message"] == f"Unknown field: {field}"
    assert inserts == []


async def test_malformed_attendee_rejects_entire_write(monkeypatch):
    result, _connector, inserts = await _execute(
        monkeypatch,
        {
            **_VALID_INPUT,
            "attendees": ["valid@example.com", "not-an-email"],
        },
    )
    assert result["ok"] is False
    assert "isn't a valid attendee email" in result["user_message"]
    assert inserts == []


async def test_google_body_timezone_attendees_and_send_updates(monkeypatch):
    result, connector, inserts = await _execute(
        monkeypatch,
        {
            "title": "Lunch",
            "when": "tomorrow at 9 AM for 45 minutes",
            "description": "Quarterly planning",
            "location": "Room 4",
            "attendees": [
                "Sam@Example.com",
                "sam@example.com",
                "kim@example.com",
            ],
        },
    )

    assert result == {
        "ok": True,
        "configured": True,
        "event_id": inserts[0]["body"]["id"],
        "html_link": "https://calendar.test/event",
        "status": "confirmed",
        "invited_count": 2,
        "timezone": "America/Los_Angeles",
        "start_local": "2026-07-29T09:00:00-07:00",
        "end_local": "2026-07-29T09:45:00-07:00",
        "start_utc": "2026-07-29T16:00:00+00:00",
        "end_utc": "2026-07-29T16:45:00+00:00",
    }
    assert len(inserts) == 1
    assert inserts[0]["calendarId"] == "primary"
    assert inserts[0]["sendUpdates"] == "all"
    assert inserts[0]["body"] == {
        "id": inserts[0]["body"]["id"],
        "summary": "Lunch",
        "description": "Quarterly planning",
        "location": "Room 4",
        "start": {
            "dateTime": "2026-07-29T16:00:00+00:00",
            "timeZone": "America/Los_Angeles",
        },
        "end": {
            "dateTime": "2026-07-29T16:45:00+00:00",
            "timeZone": "America/Los_Angeles",
        },
        "attendees": [
            {"email": "sam@example.com"},
            {"email": "kim@example.com"},
        ],
    }
    connector.cache_api_events.assert_called_once()


@pytest.mark.parametrize(
    ("attendees", "expected_count", "expected_say"),
    [
        ([], 0, 'Done, I added "Lunch" to your calendar.'),
        (
            ["sam@example.com"],
            1,
            'Done, I added "Lunch" and invited 1 guest.',
        ),
        (
            ["Sam@example.com", "sam@example.com", "kim@example.com"],
            2,
            'Done, I added "Lunch" and invited 2 guests.',
        ),
    ],
)
async def test_mcp_confirmation_uses_canonical_invited_count(
    monkeypatch,
    attendees: list[str],
    expected_count: int,
    expected_say: str,
):
    connector, inserts = _fake_connector()
    executor = ToolExecutor(
        "uid-1",
        created_via="voice",
        session_id="voice-session-1",
    )
    monkeypatch.setattr(
        tool_executor_module,
        "GoogleCalendarConnector",
        lambda _uid: connector,
    )
    monkeypatch.setattr(
        tool_executor_module,
        "_get_user_timezone",
        AsyncMock(return_value="America/Los_Angeles"),
    )
    monkeypatch.setattr(mcp, "_executor_for_request", lambda: executor)
    _freeze(monkeypatch, _NOW)

    result = await mcp.create_calendar_event(
        title="Lunch",
        when="tomorrow at noon",
        description="",
        location="",
        attendees=attendees,
    )

    assert len(inserts) == 1
    assert len(inserts[0]["body"].get("attendees", [])) == expected_count
    assert result["invited_count"] == expected_count
    assert result["say"] == expected_say
    assert "invited 3 guests" not in result["say"]


async def test_empty_attendees_omits_notifications_and_creates_once(monkeypatch):
    result, _connector, inserts = await _execute(monkeypatch, dict(_VALID_INPUT))
    assert result["ok"] is True
    assert len(inserts) == 1
    assert "attendees" not in inserts[0]["body"]
    assert "sendUpdates" not in inserts[0]


async def test_equivalent_natural_times_produce_same_canonical_event_id(
    monkeypatch,
):
    first, _connector, first_inserts = await _execute(
        monkeypatch,
        {**_VALID_INPUT, "when": "tomorrow at nine AM"},
    )
    second, _connector, second_inserts = await _execute(
        monkeypatch,
        {**_VALID_INPUT, "when": "tomorrow at 9 AM"},
    )
    assert first["event_id"] == second["event_id"]
    assert first["start_utc"] == second["start_utc"]
    assert first["end_utc"] == second["end_utc"]
    assert first_inserts[0]["body"]["id"] == second_inserts[0]["body"]["id"]


@pytest.mark.parametrize("failure_mode", ["missing", "fetch_error"])
async def test_calendar_timezone_failures_are_neutral_and_write_nothing(
    monkeypatch,
    failure_mode: str,
):
    connector, inserts = _fake_connector()
    document = MagicMock()
    if failure_mode == "fetch_error":
        document.get.side_effect = RuntimeError("firestore unavailable")
    else:
        snapshot = MagicMock()
        snapshot.to_dict.return_value = {}
        document.get.return_value = snapshot
    database = MagicMock()
    database.collection.return_value.document.return_value = document
    monkeypatch.setattr(tool_executor_module, "admin_firestore", lambda: database)
    monkeypatch.setattr(
        tool_executor_module,
        "GoogleCalendarConnector",
        lambda _uid: connector,
    )

    result = await ToolExecutor("uid-1").execute(
        "create_calendar_event",
        dict(_VALID_INPUT),
    )

    assert result["ok"] is False
    assert result["code"] == "validation_error"
    assert "timezone" in result["user_message"].lower()
    assert "reminder" not in result["user_message"].lower()
    assert inserts == []


def test_attendee_validation_deduplicates_without_discarding_malformed_values():
    assert _normalize_attendee_emails(
        ["Sam@Example.com", "sam@example.com", " kim@example.com "]
    ) == ["sam@example.com", "kim@example.com"]
    assert _normalize_attendee_emails([]) == []
    with pytest.raises(ValueError, match="isn't a valid attendee email"):
        _normalize_attendee_emails(["sam@example.com", "not-an-email"])


def test_calendar_write_guidance_lives_only_in_the_tool_description():
    description = CREATE_CALENDAR_EVENT_TOOL_DEFINITION["description"]
    assert "only when the user asks to create or schedule" in description
    assert "Do not use for calendar reads" in description
    assert VOICE_TOOL_REGISTRY["create_calendar_event"].required_fields == frozenset(
        CREATE_CALENDAR_EVENT_TOOL_DEFINITION["inputSchema"]["required"]
    )
    assert VOICE_TOOL_REGISTRY[
        "create_calendar_event"
    ].empty_allowed_fields == frozenset({"description", "location", "attendees"})
    read_description = tool_definition("get_upcoming_events")["description"]
    assert "Never create an event from a read request" in read_description
