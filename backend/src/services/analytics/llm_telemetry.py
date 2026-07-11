"""
Fail-safe LLM + tool-call telemetry to Langfuse, feeding the ops dashboard's
cost-per-model and tool-call-analytics panels.

Contract (mirrors posthog_client.py):
  - NEVER raises into, or blocks, a caller. A Langfuse outage, a missing key, an
    import error, or a bad payload degrades to a no-op plus a log line.
  - Both keys unset (dev, tests) -> every call is a silent no-op object.
  - PRIVACY: prompt/completion text is NEVER sent. Only model id, provider,
    caller label, token usage, success/error, and latency (span duration) leave
    the backend. Langfuse infers cost from model id + usage for the standard
    claude-*/gpt-*/gemini-* price tables.

Usage — one recording per ACTUAL provider API attempt (fallback hops each record
their own attempt, so nothing is double-counted):

    recording = start_llm_generation(model=model_id, provider="anthropic", caller="expert")
    ... provider call ...
    recording.finish(tokens={"input": usage.input_tokens, "output": usage.output_tokens})
    # or on failure:
    recording.finish(success=False, error_type=type(exc).__name__)

    recording = start_tool_span(tool_name="track_topic", source="voice", uid=uid)
    ... tool body ...
    recording.finish(success=True)

The SDK batches in a background thread and flushes atexit; ``flush()`` is
exposed for explicit drains (tests, shutdown hooks).
"""

from __future__ import annotations

from typing import Any

from ...config.settings import settings
from ...lib.logger import logger

# Memoised client. _init_attempted ensures we only try (and only log) once.
_client: Any | None = None
_init_attempted = False


def _get_client() -> Any | None:
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True

    if not settings.langfuse_configured:
        logger.info("llm_telemetry: no LANGFUSE keys, LLM observability disabled")
        return None

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )
        logger.info("llm_telemetry: initialised", {"host": settings.LANGFUSE_HOST})
    except Exception as exc:
        logger.warn("llm_telemetry: init failed, LLM observability disabled", {
            "error": str(exc),
        })
        _client = None
    return _client


class _NoopRecording:
    """Returned when telemetry is unconfigured or broken; absorbs finish() silently."""

    def finish(self, **_: Any) -> None:
        return


_NOOP_RECORDING = _NoopRecording()


class _Recording:
    """A live Langfuse observation. finish() stamps usage/outcome and ends the
    span, so the observation's duration is the real wall-clock latency of the
    provider call or tool body it wraps. Every step is swallowed on failure."""

    def __init__(self, observation: Any) -> None:
        self._observation = observation
        self._finished = False

    def finish(
        self,
        *,
        tokens: dict[str, int] | None = None,
        success: bool = True,
        error_type: str | None = None,
    ) -> None:
        """``tokens`` uses Langfuse usage-detail names directly: ``input``,
        ``output``, and optionally ``cache_read_input_tokens`` /
        ``cache_creation_input_tokens`` (Anthropic cache pricing keys on these
        exact names). Idempotent: a second finish() is a no-op, so a call site
        may finish on success and again in a broad error handler safely."""
        if self._finished:
            return
        self._finished = True
        try:
            update_kwargs: dict[str, Any] = {
                "level": "DEFAULT" if success else "ERROR",
            }
            if error_type:
                update_kwargs["status_message"] = error_type
            if tokens:
                update_kwargs["usage_details"] = {
                    key: int(value or 0) for key, value in tokens.items()
                }
            self._observation.update(**update_kwargs)
        except Exception as exc:
            logger.debug("llm_telemetry: observation update failed", {"error": str(exc)})
        finally:
            try:
                self._observation.end()
            except Exception:
                pass


def _start_observation(client: Any, *, name: str, as_type: str, **kwargs: Any) -> Any:
    """Create a standalone observation across langfuse 3.x API variants.

    Newer 3.x exposes ``start_observation(as_type=...)``; earlier 3.x only has
    ``start_generation`` / ``start_span``. Prefer the current API, fall back to
    the older pair so a resolver picking an early 3.x still records.
    """
    start_observation = getattr(client, "start_observation", None)
    if callable(start_observation):
        return start_observation(name=name, as_type=as_type, **kwargs)
    if as_type == "generation":
        return client.start_generation(name=name, **kwargs)
    return client.start_span(name=name, **kwargs)


def start_llm_generation(
    *,
    model: str,
    provider: str,
    caller: str,
    uid: str | None = None,
) -> _Recording | _NoopRecording:
    """Open one generation for one provider API attempt. ``caller`` is the tier
    or path label the ops dashboard groups by (cheap / balanced / expert /
    grounded / reason_turn / chat / chat_gemini_fallback / chat_openai_fallback /
    voice_session). Returns a no-op object when telemetry is unavailable."""
    client = _get_client()
    if client is None:
        return _NOOP_RECORDING
    try:
        metadata: dict[str, Any] = {"provider": provider, "caller": caller}
        if uid:
            metadata["uid"] = uid
        observation = _start_observation(
            client,
            name=f"llm:{caller}",
            as_type="generation",
            model=model,
            metadata=metadata,
        )
        return _Recording(observation)
    except Exception as exc:
        logger.warn("llm_telemetry: start_llm_generation failed", {"error": str(exc)})
        return _NOOP_RECORDING


def start_tool_span(
    *,
    tool_name: str,
    source: str,
    uid: str | None = None,
) -> _Recording | _NoopRecording:
    """Open one span per tool call, named ``tool:{tool_name}`` so the ops
    dashboard can aggregate by name. ``source`` is text | voice | keyboard."""
    client = _get_client()
    if client is None:
        return _NOOP_RECORDING
    try:
        metadata: dict[str, Any] = {"source": source}
        if uid:
            metadata["uid"] = uid
        observation = _start_observation(
            client,
            name=f"tool:{tool_name}",
            as_type="span",
            metadata=metadata,
        )
        return _Recording(observation)
    except Exception as exc:
        logger.warn("llm_telemetry: start_tool_span failed", {"error": str(exc)})
        return _NOOP_RECORDING


def anthropic_usage_tokens(usage: Any) -> dict[str, int]:
    """Token usage off an Anthropic response.usage, in Langfuse usage-detail
    names for finish(tokens=...). Fully defensive: a missing/None usage object
    yields zeros, never raises."""
    tokens = {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
    }
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    if cache_read:
        tokens["cache_read_input_tokens"] = cache_read
    if cache_creation:
        tokens["cache_creation_input_tokens"] = cache_creation
    return tokens


def gemini_usage_tokens(usage_metadata: Any) -> dict[str, int]:
    """Token usage off a Gemini response.usage_metadata, in Langfuse
    usage-detail names for finish(tokens=...). Thinking tokens bill as output
    tokens, so they are folded into output. Gemini's prompt_token_count INCLUDES
    cached tokens (unlike Anthropic), and Langfuse prices each usage-detail key
    separately, so cached tokens are subtracted out of input to avoid
    double-billing. Fully defensive."""
    candidates = int(getattr(usage_metadata, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(usage_metadata, "thoughts_token_count", 0) or 0)
    prompt = int(getattr(usage_metadata, "prompt_token_count", 0) or 0)
    cached = int(getattr(usage_metadata, "cached_content_token_count", 0) or 0)
    tokens = {
        "input": max(0, prompt - cached),
        "output": candidates + thoughts,
    }
    if cached:
        tokens["cache_read_input_tokens"] = cached
    return tokens


def openai_usage_tokens(usage: Any) -> dict[str, int]:
    """Token usage off an OpenAI chat-completions usage, in Langfuse
    usage-detail names for finish(tokens=...). OpenAI's prompt_tokens INCLUDES
    cached tokens, so they are subtracted out of input (same reasoning as the
    Gemini helper). Fully defensive."""
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    tokens = {
        "input": max(0, prompt - cached),
        "output": int(getattr(usage, "completion_tokens", 0) or 0),
    }
    if cached:
        tokens["cache_read_input_tokens"] = cached
    return tokens


def flush() -> None:
    """Drain the SDK's background queue. Called from shutdown hooks and tests;
    never raises. On Cloud Run the atexit hook covers clean shutdowns, but an
    explicit flush after batch work (like the voice session close) is cheap
    insurance against instance freezes."""
    if not _init_attempted or _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:
        logger.warn("llm_telemetry: flush failed", {"error": str(exc)})
