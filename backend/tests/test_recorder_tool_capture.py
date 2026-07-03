"""Regression test for voice tool-call capture (Step 2.3a).

voice_sessions.tool_calls_made was empty on every session because the old path read
item.tool_calls, which is absent on ChatMessage items on the gpt-4.1-mini stack. Capture
now comes from the function_tools_executed event (FunctionCall.name). This locks the new
_on_tools_executed handler.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.agent.voice.recorder import VoiceSessionRecorder


def _make_recorder() -> VoiceSessionRecorder:
    # _on_tools_executed only reads session_id/user_id and appends to tool_calls, so the
    # session and ctx can be inert stand-ins.
    return VoiceSessionRecorder(
        session=SimpleNamespace(),
        ctx=SimpleNamespace(),
        session_id="sess-1",
        user_id="user-1",
        user_tier="free",
    )


def test_captures_tool_names_in_order():
    rec = _make_recorder()
    ev = SimpleNamespace(
        function_calls=[SimpleNamespace(name="web_surf"), SimpleNamespace(name="set_reminder")]
    )
    rec._on_tools_executed(ev)
    assert rec.tool_calls == ["web_surf", "set_reminder"]


def test_accumulates_across_multiple_events():
    rec = _make_recorder()
    rec._on_tools_executed(SimpleNamespace(function_calls=[SimpleNamespace(name="web_surf")]))
    rec._on_tools_executed(SimpleNamespace(function_calls=[SimpleNamespace(name="query_memory")]))
    assert rec.tool_calls == ["web_surf", "query_memory"]


def test_handles_empty_or_missing_function_calls():
    rec = _make_recorder()
    rec._on_tools_executed(SimpleNamespace(function_calls=[]))
    rec._on_tools_executed(SimpleNamespace())  # attribute absent entirely
    assert rec.tool_calls == []


def test_skips_entries_without_a_name():
    rec = _make_recorder()
    ev = SimpleNamespace(
        function_calls=[
            SimpleNamespace(name=""),
            SimpleNamespace(name="query_memory"),
            SimpleNamespace(),  # no name attribute at all
        ]
    )
    rec._on_tools_executed(ev)
    assert rec.tool_calls == ["query_memory"]
