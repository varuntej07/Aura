from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from livekit.agents import llm as lk_llm
from livekit.agents.llm._provider_format.openai import to_fnc_ctx

from src.agent import buddy_agent as buddy_agent_module
from src.agent.buddy_agent import BuddyAgent
from src.agent.voice.capabilities import VOICE_TOOL_REGISTRY
from src.prompts import MOBILE_VOICE_SYSTEM_PROMPT
from src.handlers import mcp
from src.services.feedback import feedback_capture
from src.services.feedback.feedback_schema import (
    FEEDBACK_ABOUT_AREAS,
    FEEDBACK_CATEGORIES,
    FEEDBACK_SEVERITIES,
    FEEDBACK_TOOL_NAME,
    FIELD_QUOTE,
    VOICE_FEEDBACK_TOOL_DEFINITION,
    FeedbackReport,
    voice_feedback_document_id,
)
from src.services.tool_executor import ToolExecutor
from src.shared.tools import tool_definition


def _context_vars() -> dict[str, str]:
    return {
        "name": "V",
        "timezone": "America/Los_Angeles",
        "local_time": "8:00 PM",
        "local_date": "July 28, 2026",
        "memory_summary": "",
        "graph_context": "",
        "last_session_context": "",
        "archive_context": "",
        "user_aura_profile": "",
    }


def _agent() -> BuddyAgent:
    return BuddyAgent(
        user_id="uid-voice-7",
        context_vars=_context_vars(),
        chat_ctx=lk_llm.ChatContext(),
        session_id="voice-session-9",
    )


def _valid_arguments() -> dict[str, object]:
    return {
        "category": "complaint",
        "about": "voice",
        "summary": "The voice interruption handling feels unreliable.",
        "severity": "high",
    }


def _finalize(
    agent: BuddyAgent,
    *,
    transcript: str = "The Voice cut me OFF, twice!",
    message_id: str = "finalized-message-1",
) -> None:
    agent._finalized_transcript = transcript
    agent._finalized_message_id = message_id


def _expected_openai_schema() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "report_feedback",
                "description": (
                    "Silently record feedback about Aura itself. Use for complaints, bugs, "
                    "confusion, feature requests, praise, or signs the user may stop using "
                    "Aura, including when the user explicitly asks you to report or file "
                    "something. Do not use for ordinary requests, factual questions, or "
                    "conversation unrelated to the product. Continue the spoken reply "
                    "normally and never mention that feedback was recorded."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": FEEDBACK_CATEGORIES,
                            "description": "The kind of product feedback.",
                        },
                        "about": {
                            "type": "string",
                            "enum": FEEDBACK_ABOUT_AREAS,
                            "description": "Which part of Aura the feedback concerns.",
                        },
                        "summary": {
                            "type": "string",
                            "description": (
                                "One short founder-readable sentence capturing the feedback."
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "enum": FEEDBACK_SEVERITIES,
                            "description": (
                                "How strongly the user feels or how urgent the problem is."
                            ),
                        },
                    },
                    "required": ["category", "about", "summary", "severity"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
    ]


def test_actual_livekit_openai_serialization_is_exact_four_field_schema():
    agent = _agent()

    serialized = to_fnc_ctx(
        lk_llm.ToolContext([agent.report_feedback]),
        strict=True,
    )

    assert serialized == _expected_openai_schema()
    assert VOICE_FEEDBACK_TOOL_DEFINITION == _expected_openai_schema()[0]["function"]


async def test_fastmcp_discovery_excludes_feedback_and_combined_inventory_has_one():
    fastmcp_names = [tool.name for tool in await mcp.mcp_server.list_tools()]
    local_names = [tool.info.name for tool in _agent().tools]
    combined_names = fastmcp_names + local_names

    assert FEEDBACK_TOOL_NAME not in fastmcp_names
    assert combined_names.count(FEEDBACK_TOOL_NAME) == 1
    assert len(combined_names) == len(VOICE_TOOL_REGISTRY)
    assert set(combined_names) == set(VOICE_TOOL_REGISTRY)


@pytest.mark.parametrize("unknown_field", ["verbatim_quote", "anything_else"])
async def test_legacy_quote_and_every_unknown_field_reject_with_zero_writes(
    monkeypatch,
    unknown_field: str,
):
    capture = AsyncMock(return_value=True)
    monkeypatch.setattr(buddy_agent_module, "capture_feedback", capture)
    agent = _agent()
    _finalize(agent)

    with pytest.raises(lk_llm.ToolError, match="unknown field"):
        await agent.report_feedback(
            {**_valid_arguments(), unknown_field: "model supplied text"}
        )

    capture.assert_not_awaited()


@pytest.mark.parametrize("missing_field", ["category", "about", "summary", "severity"])
async def test_every_missing_field_rejects_with_zero_writes(
    monkeypatch,
    missing_field: str,
):
    capture = AsyncMock(return_value=True)
    monkeypatch.setattr(buddy_agent_module, "capture_feedback", capture)
    arguments = _valid_arguments()
    del arguments[missing_field]
    agent = _agent()
    _finalize(agent)

    with pytest.raises(lk_llm.ToolError, match="missing required field"):
        await agent.report_feedback(arguments)

    capture.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "rage"),
        ("about", "payments"),
        ("severity", "critical"),
        ("category", 7),
        ("about", True),
        ("summary", ["not", "a", "string"]),
        ("severity", None),
    ],
)
async def test_invalid_enum_and_non_string_values_reject_with_zero_writes(
    monkeypatch,
    field: str,
    value: object,
):
    capture = AsyncMock(return_value=True)
    monkeypatch.setattr(buddy_agent_module, "capture_feedback", capture)
    arguments = _valid_arguments()
    arguments[field] = value
    agent = _agent()
    _finalize(agent)

    with pytest.raises(lk_llm.ToolError):
        await agent.report_feedback(arguments)

    capture.assert_not_awaited()


@pytest.mark.parametrize(
    ("transcript", "message_id", "error"),
    [
        ("", "message-1", "non-empty finalized transcript"),
        ("   ", "message-1", "non-empty finalized transcript"),
        ("valid transcript", "", "finalized message ID"),
    ],
)
async def test_missing_finalized_context_rejects_with_zero_writes(
    monkeypatch,
    transcript: str,
    message_id: str,
    error: str,
):
    capture = AsyncMock(return_value=True)
    monkeypatch.setattr(buddy_agent_module, "capture_feedback", capture)
    agent = _agent()
    _finalize(agent, transcript=transcript, message_id=message_id)

    with pytest.raises(lk_llm.ToolError, match=error):
        await agent.report_feedback(_valid_arguments())

    capture.assert_not_awaited()


async def test_exact_finalized_transcript_and_voice_context_reach_persistence(
    monkeypatch,
):
    calls: list[dict[str, object]] = []

    async def _capture(
        uid: str,
        report: FeedbackReport,
        *,
        source: str,
        session_id: str | None,
        document_id: str | None,
    ) -> bool:
        calls.append(
            {
                "uid": uid,
                "report": report,
                "source": source,
                "session_id": session_id,
                "document_id": document_id,
            }
        )
        return True

    monkeypatch.setattr(buddy_agent_module, "capture_feedback", _capture)
    transcript = "The Voice cut me OFF, twice!"
    agent = _agent()
    _finalize(agent, transcript=transcript)

    result = await agent.report_feedback(_valid_arguments())

    assert result["ok"] is True
    assert calls[0]["uid"] == "uid-voice-7"
    assert calls[0]["source"] == "voice"
    assert calls[0]["session_id"] == "voice-session-9"
    assert calls[0]["report"].verbatim_quote == transcript
    assert calls[0]["document_id"] == voice_feedback_document_id(
        uid="uid-voice-7",
        session_id="voice-session-9",
        finalized_message_id="finalized-message-1",
    )


async def test_duplicate_message_creates_one_document_and_later_message_creates_second(
    monkeypatch,
):
    documents: dict[str, dict[str, object]] = {}
    capture_calls = 0

    async def _capture(
        uid: str,
        report: FeedbackReport,
        *,
        source: str,
        session_id: str | None,
        document_id: str | None,
    ) -> bool:
        nonlocal capture_calls
        capture_calls += 1
        assert document_id is not None
        documents[document_id] = {
            "uid": uid,
            "source": source,
            "session_id": session_id,
            FIELD_QUOTE: report.verbatim_quote,
        }
        return True

    monkeypatch.setattr(buddy_agent_module, "capture_feedback", _capture)
    agent = _agent()
    _finalize(agent)

    first = await agent.report_feedback(_valid_arguments())
    duplicate = await agent.report_feedback(_valid_arguments())
    _finalize(
        agent,
        transcript="The latest reply is much better.",
        message_id="finalized-message-2",
    )
    later = await agent.report_feedback(
        {
            "category": "praise",
            "about": "voice",
            "summary": "The latest voice reply was better.",
            "severity": "low",
        }
    )

    assert first["ok"] is duplicate["ok"] is later["ok"] is True
    assert capture_calls == 2
    assert len(documents) == 2
    assert {document[FIELD_QUOTE] for document in documents.values()} == {
        "The Voice cut me OFF, twice!",
        "The latest reply is much better.",
    }


async def test_failed_persistence_is_silent_truthful_and_retryable(monkeypatch):
    outcomes = iter((False, True))
    capture = AsyncMock(side_effect=lambda *_args, **_kwargs: next(outcomes))
    monkeypatch.setattr(buddy_agent_module, "capture_feedback", capture)
    agent = _agent()
    _finalize(agent)

    failed = await agent.report_feedback(_valid_arguments())
    retried = await agent.report_feedback(_valid_arguments())
    duplicate_after_success = await agent.report_feedback(_valid_arguments())

    assert failed["ok"] is False
    assert retried["ok"] is True
    assert duplicate_after_success["ok"] is True
    assert capture.await_count == 2
    for result in (failed, retried, duplicate_after_success):
        assert "say" not in result
        assert "render" not in result
        assert "card" not in result
        assert "voice" not in result
        # The envelope, not the description, is what silences the post-call speech.
        assert "Say nothing about feedback" in result["then"]


def test_voice_feedback_identity_is_stable_per_finalized_message():
    first = voice_feedback_document_id(
        uid="uid-1",
        session_id="session-1",
        finalized_message_id="message-1",
    )
    duplicate = voice_feedback_document_id(
        uid="uid-1",
        session_id="session-1",
        finalized_message_id="message-1",
    )
    later = voice_feedback_document_id(
        uid="uid-1",
        session_id="session-1",
        finalized_message_id="message-2",
    )

    assert first == duplicate
    assert first != later


async def test_text_chat_schema_and_feedback_execution_keep_verbatim_quote(
    monkeypatch,
):
    definition = tool_definition(FEEDBACK_TOOL_NAME)
    assert definition is not None
    assert list(definition["inputSchema"]["properties"]) == [
        "category",
        "about",
        "summary",
        "verbatim_quote",
        "severity",
    ]
    assert definition["inputSchema"]["required"] == [
        "category",
        "about",
        "summary",
        "verbatim_quote",
    ]
    assert definition["inputSchema"]["properties"]["severity"]["default"] == "medium"

    calls: list[dict[str, object]] = []

    async def _capture(
        uid: str,
        report: FeedbackReport,
        *,
        source: str,
        session_id: str | None,
        document_id: str | None = None,
    ) -> bool:
        calls.append(
            {
                "uid": uid,
                "report": report,
                "source": source,
                "session_id": session_id,
                "document_id": document_id,
            }
        )
        return True

    monkeypatch.setattr(feedback_capture, "capture_feedback", _capture)
    result = await ToolExecutor("uid-text-2", created_via="text").execute(
        FEEDBACK_TOOL_NAME,
        {
            "category": "bug",
            "about": "chat",
            "summary": "Chat lost the response.",
            "verbatim_quote": "It just ate my reply!",
            "severity": "high",
        },
    )

    assert result["ok"] is True
    assert result["recorded"] is True
    assert calls[0]["uid"] == "uid-text-2"
    assert calls[0]["source"] == "text"
    assert calls[0]["session_id"] is None
    assert calls[0]["document_id"] is None
    assert calls[0]["report"].verbatim_quote == "It just ate my reply!"


async def test_text_chat_reports_durable_failure_truthfully(monkeypatch):
    monkeypatch.setattr(
        feedback_capture,
        "capture_feedback",
        AsyncMock(return_value=False),
    )

    result = await ToolExecutor("uid-text-2", created_via="text").execute(
        FEEDBACK_TOOL_NAME,
        {
            "category": "bug",
            "about": "chat",
            "summary": "Chat lost the response.",
            "verbatim_quote": "It just ate my reply!",
            "severity": "high",
        },
    )

    assert result["ok"] is False
    assert result["recorded"] is False


def test_capability_fields_match_serialized_schema_and_parallel_policy():
    registration = VOICE_TOOL_REGISTRY[FEEDBACK_TOOL_NAME]
    required = _expected_openai_schema()[0]["function"]["parameters"]["required"]

    assert registration.required_fields == frozenset(required)
    assert registration.safe_concurrently is True


def test_feedback_added_no_resident_voice_prompt_text():
    assert FEEDBACK_TOOL_NAME not in MOBILE_VOICE_SYSTEM_PROMPT
