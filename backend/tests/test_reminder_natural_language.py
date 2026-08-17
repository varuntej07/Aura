from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from livekit.agents import llm as lk_llm
from livekit.agents.llm._provider_format.openai import to_fnc_ctx
from mcp.server.fastmcp.exceptions import ToolError

import src.services.analytics.posthog_client as posthog_client
import src.services.reminder_time as reminder_time
import src.services.threads.thread_writer as thread_writer
import src.services.tool_executor as tool_executor_module
from src.agent.voice.pipelines import AuraMCPServerHTTP
from src.handlers import mcp
from src.services.openai_chat_fallback import _anthropic_tools_to_openai
from src.services.reminder_time import parse_reminder_when
from src.services.tool_executor import ToolExecutor
from src.shared.tools import (
    SET_REMINDER_TOOL_DEFINITION,
    claude_tool_definitions,
    openai_function_definition,
)

_NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
_ACCEPTED_CASES = [
    (
        "tomorrow at nine AM",
        "2026-07-29T09:00:00-07:00",
        datetime(2026, 7, 29, 16, 0, tzinfo=UTC),
    ),
    (
        "in forty-five minutes",
        "2026-07-28T13:45:00-07:00",
        datetime(2026, 7, 28, 20, 45, tzinfo=UTC),
    ),
    (
        "at noon tomorrow",
        "2026-07-29T12:00:00-07:00",
        datetime(2026, 7, 29, 19, 0, tzinfo=UTC),
    ),
    (
        "this Friday at 3 PM",
        "2026-07-31T15:00:00-07:00",
        datetime(2026, 7, 31, 22, 0, tzinfo=UTC),
    ),
]


class _FakeDoc:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


class _FakeDocRef:
    def __init__(self, store: dict, doc_id: str):
        self._store = store
        self.id = doc_id

    def set(self, data: dict) -> None:
        self._store[self.id] = dict(data)


class _FakeQuery:
    def __init__(self, store: dict):
        self._store = store

    def where(self, *, filter=None):  # noqa: A002
        return self

    def stream(self):
        return [
            _FakeDoc(doc_id, data)
            for doc_id, data in self._store.items()
            if data.get("status") == "pending"
        ]


class _FakeCollection:
    def __init__(self, store: dict):
        self._store = store

    def where(self, *, filter=None):  # noqa: A002
        return _FakeQuery(self._store)

    def document(self, doc_id: str):
        return _FakeDocRef(self._store, doc_id)


def _freeze(monkeypatch, instant: datetime) -> None:
    monkeypatch.setattr(reminder_time, "_utc_now", lambda: instant)


def test_set_reminder_schema_is_exact_and_strict_for_openai():
    anthropic_tool = next(
        tool for tool in claude_tool_definitions() if tool["name"] == "set_reminder"
    )
    schema = anthropic_tool["input_schema"]
    # OpenAI strict mode requires every property to appear in `required`.
    # `tone=""` remains the explicit value for using the user's own choice.
    assert set(schema["properties"]) == {"message", "when", "tier", "tone"}
    assert schema["required"] == ["message", "when", "tier", "tone"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["message"] == {
        "type": "string",
        "description": "What to remind the user about.",
    }
    assert schema["properties"]["when"]["type"] == "string"
    assert schema["properties"]["tier"]["enum"] == ["reminder", "alarm"]

    openai_tool = _anthropic_tools_to_openai([anthropic_tool])[0]
    assert openai_tool["function"]["strict"] is True
    assert openai_tool["function"]["parameters"] == schema
    assert tuple(inspect.signature(mcp.set_reminder).parameters) == (
        "message",
        "when",
        "tier",
        "tone",
    )


async def test_voice_mcp_schema_exposes_only_both_required_fields():
    tools = await mcp.mcp_server.list_tools()
    tool = next(tool for tool in tools if tool.name == "set_reminder")
    assert tool.description == SET_REMINDER_TOOL_DEFINITION["description"]
    assert tool.inputSchema == SET_REMINDER_TOOL_DEFINITION["inputSchema"]


async def test_actual_livekit_openai_serialization_preserves_strict_contract():
    fastmcp_tools = await mcp.mcp_server.list_tools()
    fastmcp_tool = next(tool for tool in fastmcp_tools if tool.name == "set_reminder")
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

    serialized = to_fnc_ctx(lk_llm.ToolContext([livekit_tool]))

    assert serialized == [
        {
            "type": "function",
            "function": openai_function_definition("set_reminder"),
        }
    ]


async def test_real_fastmcp_validation_rejects_unknown_fields_before_creation(monkeypatch):
    run_tool = AsyncMock()
    monkeypatch.setattr(mcp, "_run_tool", run_tool)

    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        await mcp.mcp_server.call_tool(
            "set_reminder",
            {
                "message": "Call Mom",
                "when": "tomorrow at 9 AM",
                "tier": "reminder",
                "scheduled_at": "2026-07-29T16:00:00Z",
                "priority": "high",
            },
        )

    run_tool.assert_not_awaited()


@pytest.mark.parametrize("legacy_field", ["scheduled_at", "priority"])
async def test_legacy_set_reminder_fields_are_rejected(legacy_field: str):
    result = await ToolExecutor("u1").execute(
        "set_reminder",
        {
            "message": "Call Mom",
            "when": "tomorrow at 9 AM",
            "tier": "reminder",
            legacy_field: "old",
        },
    )
    assert result["code"] == "validation_error"
    # The schema string is diagnostic, not speech. Buddy says user_message close to
    # verbatim, so the offending field name stays in `debug` where the model and the
    # logs can use it and the user never hears it.
    assert result["debug"] == f"Unknown field: {legacy_field}"
    assert legacy_field not in result["user_message"]


@pytest.mark.parametrize(("when", "expected_local", "expected_utc"), _ACCEPTED_CASES)
def test_accepted_voice_forms_map_to_exact_local_and_utc_times(
    monkeypatch,
    when: str,
    expected_local: str,
    expected_utc: datetime,
):
    _freeze(monkeypatch, _NOW)
    parsed = parse_reminder_when(when, "America/Los_Angeles")
    assert parsed.local.isoformat() == expected_local
    assert parsed.utc == expected_utc
    assert parsed.timezone == "America/Los_Angeles"


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("tomorrow", "exact time"),
        ("tomorrow at 9", "AM or PM"),
        ("08/09 at 3 PM", "numeric date"),
        ("next Friday at 3 PM", "calendar date"),
        ("not a real time at 3 PM", "couldn't understand"),
        ("tomorrow at 9 AM and Friday at 3 PM", "complete time"),
        ("2026-07-29T09:00:00-07:00", "natural time"),
    ],
)
def test_ambiguous_or_invalid_time_returns_actionable_clarification(
    monkeypatch,
    when: str,
    expected: str,
):
    _freeze(monkeypatch, datetime(2026, 7, 28, 20, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match=expected):
        parse_reminder_when(when, "America/Los_Angeles")


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("March 8, 2026 at 2:30 AM", "doesn't exist"),
        ("November 1, 2026 at 1:30 AM", "happens twice"),
        ("July 27 at 9 AM", "already passed"),
        ("today at 9 AM", "already passed"),
    ],
)
def test_dst_and_past_times_return_actionable_clarification(
    monkeypatch,
    when: str,
    expected: str,
):
    now = (
        datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        if when.startswith("March")
        else datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        if when.startswith("November")
        else datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
    )
    _freeze(monkeypatch, now)
    with pytest.raises(ValueError, match=expected):
        parse_reminder_when(when, "America/Los_Angeles")


@pytest.mark.parametrize(("when", "expected_local", "expected_utc"), _ACCEPTED_CASES)
async def test_each_accepted_form_creates_exactly_one_canonical_reminder(
    monkeypatch,
    when: str,
    expected_local: str,
    expected_utc: datetime,
):
    store: dict[str, dict] = {}
    executor = ToolExecutor("u1", created_via="text")
    monkeypatch.setattr(executor, "_reminders_ref", lambda: _FakeCollection(store))
    monkeypatch.setattr(
        tool_executor_module,
        "_get_user_timezone",
        AsyncMock(return_value="America/Los_Angeles"),
    )
    monkeypatch.setattr(thread_writer, "record_reminder_thread", AsyncMock())
    monkeypatch.setattr(posthog_client, "capture_event", AsyncMock())
    _freeze(monkeypatch, _NOW)

    result = await executor.execute(
        "set_reminder",
        {"message": "Call Mom", "when": when, "tier": "reminder"},
    )

    assert result["ok"] is True
    assert result["trigger_at"] == expected_utc.isoformat()
    assert result["timezone"] == "America/Los_Angeles"
    assert result["tier"] == "reminder"
    assert len(store) == 1
    stored = next(iter(store.values()))
    assert stored["trigger_at"] == expected_utc.isoformat()
    assert stored["message"] == "Call Mom"
    assert stored["tier"] == "reminder"
    # The wall clock the user asked for, alongside the absolute instant. An alarm
    # re-anchors to these when the device changes timezone; a reminder does not.
    assert stored["timezone"] == "America/Los_Angeles"
    assert stored["local_time"] == expected_local.split("-07:00")[0]
    assert "priority" not in stored


@pytest.mark.parametrize(
    ("when", "now", "expected"),
    [
        ("tomorrow", _NOW, "exact time"),
        ("tomorrow at 9", _NOW, "AM or PM"),
        ("08/09 at 3 PM", _NOW, "numeric date"),
        ("not a real time at 3 PM", _NOW, "couldn't understand"),
        ("March 8, 2026 at 2:30 AM", datetime(2026, 3, 1, 12, tzinfo=UTC), "doesn't exist"),
        (
            "November 1, 2026 at 1:30 AM",
            datetime(2026, 10, 1, 12, tzinfo=UTC),
            "happens twice",
        ),
        ("today at 9 AM", _NOW, "already passed"),
    ],
)
async def test_every_rejected_form_returns_clarification_without_writing(
    monkeypatch,
    when: str,
    now: datetime,
    expected: str,
):
    store: dict[str, dict] = {}
    executor = ToolExecutor("u1", created_via="voice")
    monkeypatch.setattr(executor, "_reminders_ref", lambda: _FakeCollection(store))
    monkeypatch.setattr(
        tool_executor_module,
        "_get_user_timezone",
        AsyncMock(return_value="America/Los_Angeles"),
    )
    _freeze(monkeypatch, now)

    result = await executor.execute(
        "set_reminder",
        {"message": "Call Mom", "when": when, "tier": "reminder"},
    )

    assert result["ok"] is False
    assert result["code"] == "validation_error"
    assert expected in result["user_message"]
    assert store == {}
