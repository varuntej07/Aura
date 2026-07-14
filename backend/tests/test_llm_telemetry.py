"""llm_telemetry contract tests.

The one non-negotiable property: telemetry NEVER raises into, blocks, or
alters a caller's result. Unconfigured keys (the test default) must make every
call a silent no-op, and a poisoned Langfuse observation must be swallowed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.services.analytics import llm_telemetry


@pytest.fixture(autouse=True)
def reset_telemetry_singleton():
    """The module memoises its client; reset between tests so one test's fake
    client never leaks into the next (same rule as the active-users cache)."""
    llm_telemetry._client = None
    llm_telemetry._init_attempted = False
    yield
    llm_telemetry._client = None
    llm_telemetry._init_attempted = False


@pytest.fixture(autouse=True)
def blank_langfuse_keys(monkeypatch):
    """Force the unconfigured state regardless of the developer's local .env
    (a real backend/.env with live LANGFUSE keys must not make tests talk to
    the real Langfuse project)."""
    from src.config.settings import settings
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")


class TestUnconfiguredNoop:
    def test_generation_is_noop_without_keys(self, blank_langfuse_keys):
        recording = llm_telemetry.start_llm_generation(
            model="claude-sonnet-4-6", provider="anthropic", caller="expert"
        )
        assert isinstance(recording, llm_telemetry._NoopRecording)
        recording.finish(tokens={"input": 10, "output": 5})  # must not raise

    def test_tool_span_is_noop_without_keys(self, blank_langfuse_keys):
        span = llm_telemetry.start_tool_span(tool_name="track_topic", source="voice", uid="u1")
        assert isinstance(span, llm_telemetry._NoopRecording)
        span.finish(success=False, error_type="ValueError")  # must not raise

    def test_flush_is_noop_without_client(self, blank_langfuse_keys):
        llm_telemetry.flush()  # must not raise


class TestPoisonedObservationNeverRaises:
    def _recording_with_broken_observation(self):
        observation = MagicMock()
        observation.update.side_effect = RuntimeError("langfuse exploded")
        observation.end.side_effect = RuntimeError("langfuse exploded again")
        return llm_telemetry._Recording(observation)

    def test_finish_swallows_update_and_end_failures(self):
        recording = self._recording_with_broken_observation()
        recording.finish(tokens={"input": 1, "output": 2})  # must not raise

    def test_finish_is_idempotent(self):
        observation = MagicMock()
        recording = llm_telemetry._Recording(observation)
        recording.finish(tokens={"input": 1, "output": 2})
        recording.finish(success=False, error_type="late")  # second call: no-op
        assert observation.update.call_count == 1
        assert observation.end.call_count == 1

    def test_start_swallows_broken_client(self, monkeypatch):
        broken_client = MagicMock()
        broken_client.start_observation.side_effect = RuntimeError("no network")
        broken_client.start_generation.side_effect = RuntimeError("no network")
        broken_client.start_span.side_effect = RuntimeError("no network")
        llm_telemetry._client = broken_client
        llm_telemetry._init_attempted = True
        recording = llm_telemetry.start_llm_generation(
            model="gemini-2.5-flash", provider="gemini", caller="cheap"
        )
        assert isinstance(recording, llm_telemetry._NoopRecording)
        span = llm_telemetry.start_tool_span(tool_name="web_surf", source="text")
        assert isinstance(span, llm_telemetry._NoopRecording)


class TestUsageHelpers:
    def test_anthropic_usage_is_exclusive_of_cache(self):
        usage = MagicMock(
            input_tokens=100, output_tokens=40,
            cache_read_input_tokens=250, cache_creation_input_tokens=30,
        )
        tokens = llm_telemetry.anthropic_usage_tokens(usage)
        # Anthropic's input_tokens already EXCLUDES cached tokens: no subtraction.
        assert tokens == {
            "input": 100, "output": 40,
            "cache_read_input_tokens": 250, "cache_creation_input_tokens": 30,
        }

    def test_gemini_usage_subtracts_cache_and_folds_thoughts(self):
        usage_metadata = MagicMock(
            prompt_token_count=100, candidates_token_count=10,
            thoughts_token_count=5, cached_content_token_count=30,
        )
        tokens = llm_telemetry.gemini_usage_tokens(usage_metadata)
        # Gemini's prompt count INCLUDES cache; thinking tokens bill as output.
        assert tokens == {"input": 70, "output": 15, "cache_read_input_tokens": 30}

    def test_openai_usage_subtracts_cached_prompt_tokens(self):
        usage = MagicMock(
            prompt_tokens=100, completion_tokens=20,
            prompt_tokens_details=MagicMock(cached_tokens=40),
        )
        tokens = llm_telemetry.openai_usage_tokens(usage)
        assert tokens == {"input": 60, "output": 20, "cache_read_input_tokens": 40}

    def test_helpers_survive_none_usage(self):
        assert llm_telemetry.anthropic_usage_tokens(None)["input"] == 0
        assert llm_telemetry.gemini_usage_tokens(None)["output"] == 0
        assert llm_telemetry.openai_usage_tokens(None)["input"] == 0


class TestToolExecutorIsolation:
    async def test_tool_result_survives_telemetry(self, monkeypatch):
        """A tool call's result must be identical whether telemetry works or
        not: the span object returned into ToolExecutor.execute is exercised
        end to end with the real (unconfigured -> noop) module."""
        from src.services.tool_executor import ToolExecutor

        executor = ToolExecutor(user_id="test-uid", created_via="text")

        async def fake_handler(inp):
            return {"ok": True}

        monkeypatch.setattr(
            executor, "_get_user_context", fake_handler, raising=False
        )
        result = await executor.execute("get_user_context", {})
        assert result == {"ok": True}
