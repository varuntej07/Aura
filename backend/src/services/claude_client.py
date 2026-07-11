"""
ClaudeClient — multi-turn conversation with tool-use loop via Anthropic SDK.
Used by the text /chat endpoint. The LiveKit voice agent uses livekit-plugins-anthropic.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..config.settings import settings
from ..lib.logger import logger
from ..shared.tools import claude_tool_definitions
from .analytics.llm_telemetry import anthropic_usage_tokens, start_llm_generation
from .chat_error_copy import CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE
from .gemini_chat_fallback import stream_gemini_chat_fallback
from .tool_executor import ToolExecutor

_MAX_TURNS = 6
_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0  # exponential backoff: 1s, 2s, 4s
_REQUEST_TIMEOUT_S = 30.0  # per-request HTTP timeout; APITimeoutError is retryable via APIConnectionError

# Anthropic exceptions that are worth retrying (transient / server-side)
_RETRYABLE_ERRORS = (
    anthropic.RateLimitError,        # 429
    anthropic.APIConnectionError,    # network blip
    anthropic.InternalServerError,   # 500 / 529
)

EXCLUDED_TOOLS_FOR_GENERAL_CHAT: set[str] = set()
EXCLUDED_TOOLS_FOR_AGENT_CHAT: set[str] = set()

# Tools that require Starter tier or above.
# Free users only get reminder + memory + clarification tools.
STARTER_ONLY_TOOLS: frozenset[str] = frozenset({
    "create_calendar_event",
    "get_upcoming_events",
})

# Text Claude generates before a tool call is typically a brief narration sentence.
# Anything longer than this is almost certainly the start of a final response, not narration.
_NARRATION_MAX_CHARS = 80


class ClaudeClient:
    def __init__(self, tool_executor: ToolExecutor) -> None:
        self._tool_executor = tool_executor
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=_REQUEST_TIMEOUT_S,
        )

    async def send_text_turn(
        self,
        *,
        system_prompt: str | list[dict[str, Any]],
        user_content: str | list[dict[str, Any]],
        history: list[dict[str, Any]] | None = None,
        is_agent: bool = False,
        user_tier: str = "pro",
    ) -> dict[str, Any]:
        """
        Run a full multi-turn Claude conversation until a text response
        with no tool calls is produced (or max turns exceeded).

        Args:
            user_content: The current user turn — either plain text or a list of
                          Anthropic content blocks (for messages with attachments).
            history: Optional list of prior turns [{role, content}] to prepend
                     before the current user turn. Enables multi-turn context
                     across HTTP requests. Must alternate user/assistant roles
                     and end before the current user turn.
            is_agent: When True, includes agent-only tools (e.g. web_surf).

        Returns:
            {"text": str, "tool_names": list[str]}
        """
        excluded = EXCLUDED_TOOLS_FOR_AGENT_CHAT if is_agent else EXCLUDED_TOOLS_FOR_GENERAL_CHAT
        tools = [t for t in claude_tool_definitions() if t["name"] not in excluded]
        if user_tier == "free":
            tools = [t for t in tools if t["name"] not in STARTER_ONLY_TOOLS]
        if tools:
            tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}]

        # Build message list: prior history + current user turn
        prior: list[dict[str, Any]] = history or []
        messages: list[dict[str, Any]] = [
            *prior,
            {"role": "user", "content": user_content},
        ]
        accumulated_text: list[str] = []
        tool_names_used: list[str] = []
        all_captured_tool_data: list[dict[str, Any]] = []
        turn = 0
        response: Any = None
        # Anthropic fallback chain (Sonnet -> Haiku), resolved once per request.
        model_chain = [settings.ANTHROPIC_CHAT_MODEL, settings.ANTHROPIC_CHAT_MODEL_FALLBACK]
        current_model_idx = 0

        logger.info("Claude: starting conversation", {
            "model": model_chain[current_model_idx],
            "max_tokens": settings.ANTHROPIC_MAX_TOKENS,
            "user_content_type": "blocks" if isinstance(user_content, list) else "text",
            "history_turns": len(prior),
        })

        for turn in range(_MAX_TURNS):
            turn_start = time.monotonic()
            response = None
            attempt = 0
            while response is None:
                attempt += 1
                model_id = model_chain[current_model_idx]
                logger.debug(f"Claude: API call (turn {turn + 1}/{_MAX_TURNS})", {
                    "model": model_id,
                    "messages_in_history": len(messages),
                })
                try:
                    # One telemetry generation per actual API attempt; the inner
                    # try re-raises into the existing retry/fallback handling.
                    recording = start_llm_generation(
                        model=model_id, provider="anthropic", caller="chat",
                        uid=self._tool_executor.user_id,
                    )
                    try:
                        response = await self._client.messages.create(
                            model=model_id,
                            max_tokens=settings.ANTHROPIC_MAX_TOKENS,
                            system=system_prompt,  # type: ignore[arg-type]
                            tools=tools,  # type: ignore[arg-type]
                            messages=messages,  # type: ignore[arg-type]
                        )
                    except BaseException as exc:
                        recording.finish(success=False, error_type=type(exc).__name__)
                        raise
                    recording.finish(tokens=anthropic_usage_tokens(response.usage))
                except anthropic.APIError as exc:
                    # Retry with backoff only for the transient subset (rate limit,
                    # connection blip, 5xx). Anything else -- e.g. a 400 for an
                    # exhausted Anthropic credit balance, or an auth error -- would
                    # fail identically on a retry, so skip straight to the next
                    # model in the chain instead of burning the retry budget on it.
                    if isinstance(exc, _RETRYABLE_ERRORS) and attempt < _MAX_RETRIES:
                        delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                        logger.warn("Claude: retryable error, backing off", {
                            "model": model_id,
                            "turn": turn + 1,
                            "attempt": attempt,
                            "delay_s": round(delay, 2),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        })
                        await asyncio.sleep(delay)
                        continue
                    if current_model_idx < len(model_chain) - 1:
                        current_model_idx += 1
                        attempt = 0
                        logger.warn("Claude: model exhausted, falling back", {
                            "from_model": model_id,
                            "to_model": model_chain[current_model_idx],
                            "turn": turn + 1,
                            "error_type": type(exc).__name__,
                        })
                        continue
                    logger.exception("Claude: API call failed after retries (chain exhausted)", {
                        "model": model_id,
                        "turn": turn + 1,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                    raise
                except Exception as exc:
                    logger.exception("Claude: API call failed", {
                        "model": model_id,
                        "turn": turn + 1,
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                    raise

            assert response is not None  # retry loop always raises or assigns
            turn_ms = int((time.monotonic() - turn_start) * 1000)
            logger.info(f"Claude: API response (turn {turn + 1})", {
                "model": model_chain[current_model_idx],
                "stop_reason": response.stop_reason,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                "duration_ms": turn_ms,
            })

            # Collect text from this turn
            for block in response.content:
                if block.type == "text":
                    accumulated_text.append(block.text)

            # No tool calls, break the loop
            if response.stop_reason != "tool_use":
                break

            # Collect all tool_use blocks from this turn
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # Execute all tool calls for this turn concurrently.
            # captured_tool_data accumulates raw results for surfacing to the Flutter client (e.g. set_reminder -> reminder card in chat UI)
            captured_tool_data: list[dict[str, Any]] = []

            async def _run_tool(block: Any) -> dict[str, Any]:
                tool_start = time.monotonic()
                logger.info("Claude: tool call", {
                    "tool": block.name,
                    "tool_use_id": block.id,
                    "turn": turn + 1,
                })
                try:
                    result = await self._tool_executor.execute(block.name, block.input)
                    tool_ms = int((time.monotonic() - tool_start) * 1000)
                    logger.info("Claude: tool result", {
                        "tool": block.name,
                        "duration_ms": tool_ms,
                        "result_keys": list(result.keys()) if isinstance(result, dict) else "non-dict",
                    })
                    # Capture tool results that the client needs to render UI
                    if block.name == "set_reminder" and isinstance(result, dict) and "error" not in result:
                        captured_tool_data.append({"tool": block.name, "data": result})
                except Exception as exc:
                    tool_ms = int((time.monotonic() - tool_start) * 1000)
                    logger.exception("Claude: tool execution error", {
                        "tool": block.name,
                        "error": str(exc),
                        "duration_ms": tool_ms,
                    })
                    result = {"error": str(exc)}
                tool_names_used.append(block.name)
                return {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                }

            tool_results = await asyncio.gather(*[_run_tool(b) for b in tool_use_blocks])
            all_captured_tool_data.extend(captured_tool_data)

            # Append assistant turn + tool results to history
            messages.append({"role": "assistant", "content": response.content})  # type: ignore[arg-type]
            messages.append({"role": "user", "content": list(tool_results)})
        else:
            logger.warn("Claude: max turns exceeded", {
                "max_turns": _MAX_TURNS,
                "tools_used": tool_names_used,
            })

        final_text = " ".join(accumulated_text).strip()
        logger.info("Claude: conversation complete", {
            "turns": min(turn + 1, _MAX_TURNS),
            "response_len": len(final_text),
            "tools_used": tool_names_used,
        })

        return {
            "text": final_text,
            "tool_names": tool_names_used,
            "tool_result_data": all_captured_tool_data,
        }

    async def send_text_turn_stream(
        self,
        *,
        system_prompt: str | list[dict[str, Any]],
        user_content: str | list[dict[str, Any]],
        history: list[dict[str, Any]] | None = None,
        is_agent: bool = False,
        user_tier: str = "pro",
        extra_excluded_tools: frozenset[str] = frozenset(),
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Streaming version of send_text_turn. Yields SSE-compatible event dicts:
          {"type": "text_delta",      "delta": str}
          {"type": "tool_thinking",   "message": str}
          {"type": "clarification_ui","clarification_id": str, "question": str,
                                       "options": list[str], "multi_select": bool}
          {"type": "done",            "metadata": {...}}
          {"type": "error",           "message": str}

        user_content accepts either plain text or a list of Anthropic content
        blocks (used when the user attaches images or documents to their message).
        """
        excluded = EXCLUDED_TOOLS_FOR_AGENT_CHAT if is_agent else EXCLUDED_TOOLS_FOR_GENERAL_CHAT
        tools = [
            t for t in claude_tool_definitions()
            if t["name"] not in excluded and t["name"] not in extra_excluded_tools
        ]
        if user_tier == "free":
            tools = [t for t in tools if t["name"] not in STARTER_ONLY_TOOLS]
        if tools:
            tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        prior: list[dict[str, Any]] = history or []
        messages: list[dict[str, Any]] = [*prior, {"role": "user", "content": user_content}]
        tool_names_used: list[str] = []
        all_captured_tool_data: list[dict[str, Any]] = []
        text_started = False
        model_chain = [settings.ANTHROPIC_CHAT_MODEL, settings.ANTHROPIC_CHAT_MODEL_FALLBACK]
        current_model_idx = 0

        logger.info("Claude: starting stream", {
            "model": model_chain[current_model_idx],
            "user_content_type": "blocks" if isinstance(user_content, list) else "text",
            "history_turns": len(prior),
        })

        try:
            for turn in range(_MAX_TURNS):
                response = None

                attempt = 0
                while response is None:
                    attempt += 1
                    model_id = model_chain[current_model_idx]
                    try:
                        # One telemetry generation per actual API attempt; the
                        # nested try re-raises into the existing fallback handling.
                        recording = start_llm_generation(
                            model=model_id, provider="anthropic", caller="chat",
                            uid=self._tool_executor.user_id,
                        )
                        async with self._client.messages.stream(
                            model=model_id,
                            max_tokens=settings.ANTHROPIC_MAX_TOKENS,
                            system=system_prompt,  # type: ignore[arg-type]
                            tools=tools,  # type: ignore[arg-type]
                            messages=messages,  # type: ignore[arg-type]
                        ) as stream:
                            # Per-turn buffer: holds text until we know if this is a
                            # tool-call turn (narration -> tool_thinking) or a final turn
                            # (response -> text_delta). Once buffered chars exceed
                            # _NARRATION_MAX_CHARS we commit to streaming as text_delta.
                            turn_text_buffer: list[str] = []
                            buffered_chars = 0
                            committed_to_streaming = False

                            async for event in stream:
                                if event.type == "content_block_start":
                                    if event.content_block.type == "tool_use":
                                        narration = "".join(turn_text_buffer).strip()
                                        if narration:
                                            yield {"type": "tool_thinking", "message": narration}
                                        turn_text_buffer.clear()
                                        buffered_chars = 0

                                elif event.type == "content_block_delta":
                                    if event.delta.type == "text_delta":
                                        chunk = event.delta.text
                                        if committed_to_streaming:
                                            text_started = True
                                            yield {"type": "text_delta", "delta": chunk}
                                        else:
                                            turn_text_buffer.append(chunk)
                                            buffered_chars += len(chunk)
                                            if buffered_chars >= _NARRATION_MAX_CHARS:
                                                committed_to_streaming = True
                                                for c in turn_text_buffer:
                                                    text_started = True
                                                    yield {"type": "text_delta", "delta": c}
                                                turn_text_buffer.clear()

                                elif event.type == "message_delta":
                                    if getattr(event.delta, "stop_reason", None) == "end_turn":
                                        for chunk in turn_text_buffer:
                                            text_started = True
                                            yield {"type": "text_delta", "delta": chunk}
                                        turn_text_buffer.clear()

                            response = await stream.get_final_message()
                        recording.finish(tokens=anthropic_usage_tokens(response.usage))
                        # success : `response` is set, so the while loop exits
                    except anthropic.APIError as exc:
                        # A client disconnect mid-stream (GeneratorExit) bypasses this
                        # handler and drops the unfinished recording — an accepted,
                        # rare undercount; the background-completion regen records its
                        # own turn. finish() is idempotent, so this never double-logs.
                        recording.finish(success=False, error_type=type(exc).__name__)
                        # Once a token has streamed we can neither replay it nor switch models,
                        # so propagate and let the outer handler yield one error event.
                        if text_started:
                            raise
                        # Retry with backoff only for the transient subset (rate limit,
                        # connection blip, 5xx). Anything else -- e.g. a 400 for an
                        # exhausted Anthropic credit balance, or an auth error -- would
                        # fail identically on a retry, so skip straight to the next
                        # model in the chain instead of burning the retry budget on it.
                        if isinstance(exc, _RETRYABLE_ERRORS) and attempt < _MAX_RETRIES:
                            delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                            logger.warn("Claude stream: retrying", {
                                "model": model_id,
                                "turn": turn + 1,
                                "attempt": attempt,
                                "delay_s": round(delay, 2),
                                "error": str(exc),
                            })
                            await asyncio.sleep(delay)
                            continue
                        # This model is done for this turn: either its retry budget is
                        # spent, or the error wasn't retryable in the first place.
                        if current_model_idx < len(model_chain) - 1:
                            current_model_idx += 1
                            attempt = 0
                            logger.warn("Claude stream: model exhausted, falling back", {
                                "from_model": model_id,
                                "to_model": model_chain[current_model_idx],
                                "turn": turn + 1,
                                "error_type": type(exc).__name__,
                            })
                            continue
                        # Whole Anthropic chain is down before any token reached the user —
                        # hand the rest of the conversation to the cross-provider Gemini hop,
                        # which emits the same SSE events (and its own `done`), and can itself
                        # delegate further to the GPT hop if Gemini is down too.
                        logger.warn("Claude stream: Anthropic chain exhausted, delegating to Gemini", {
                            "turn": turn + 1,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        })
                        async for gemini_event in stream_gemini_chat_fallback(
                            tool_executor=self._tool_executor,
                            system_prompt=system_prompt,
                            messages=messages,
                            tools=tools,
                            captured_tool_data=all_captured_tool_data,
                            tool_names_used=tool_names_used,
                        ):
                            yield gemini_event
                        return

                assert response is not None

                logger.info(f"Claude stream: turn {turn + 1} complete", {
                    "model": model_chain[current_model_idx],
                    "stop_reason": response.stop_reason,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                    "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                })

                if response.stop_reason != "tool_use":
                    break

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                async def _run_tool(block: Any) -> tuple[str, str, Any, Exception | None]:
                    try:
                        result = await self._tool_executor.execute(block.name, block.input)
                        return (block.id, block.name, result, None)
                    except Exception as exc:
                        logger.exception("Claude stream: tool error", {
                            "tool": block.name,
                            "error": str(exc),
                        })
                        return (block.id, block.name, None, exc)

                tool_results_raw = await asyncio.gather(*[_run_tool(b) for b in tool_use_blocks])
                for _, name, _, _ in tool_results_raw:
                    tool_names_used.append(name)

                # Check for clarification sentinel
                clarification = next(
                    (r for r in tool_results_raw
                     if isinstance(r[2], dict) and r[2].get("__clarification__")),
                    None,
                )
                if clarification:
                    _, _, clar_data, _ = clarification
                    yield {
                        "type": "clarification_ui",
                        "clarification_id": clar_data["clarification_id"],
                        "question": clar_data["question"],
                        "options": clar_data["options"],
                        "multi_select": clar_data.get("multi_select", False),
                    }
                    reminder_data = next(
                        (d["data"] for d in all_captured_tool_data if d["tool"] == "set_reminder"),
                        None,
                    )
                    metadata: dict[str, Any] = {
                        "tool_names": tool_names_used,
                        "awaiting_clarification": True,
                    }
                    if reminder_data:
                        metadata["reminder"] = reminder_data
                    yield {"type": "done", "metadata": metadata}
                    return

                # Build tool_result messages for next turn
                tool_results = []
                for tool_id, tool_name, result, exc in tool_results_raw:
                    if exc is not None:
                        content = str({"error": str(exc)})
                    else:
                        if tool_name == "set_reminder" and isinstance(result, dict) and "error" not in result:
                            all_captured_tool_data.append({"tool": tool_name, "data": result})
                        content = str(result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": content,
                    })

                messages.append({"role": "assistant", "content": response.content})  # type: ignore[arg-type]
                messages.append({"role": "user", "content": tool_results})
            else:
                logger.warn("Claude stream: max turns exceeded", {"tools_used": tool_names_used})

            reminder_data = next(
                (d["data"] for d in all_captured_tool_data if d["tool"] == "set_reminder"),
                None,
            )
            metadata = {"tool_names": tool_names_used}
            if reminder_data:
                metadata["reminder"] = reminder_data
            yield {"type": "done", "metadata": metadata}

        except Exception as exc:
            logger.exception("Claude stream: failed", {
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
            yield {"type": "error", "message": CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE}
