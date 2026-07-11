"""
openai_chat_fallback.py — third-tier cross-provider chat fallback for the text
/chat stream, using GPT (settings.OPENAI_CHAT_MODEL, already provisioned via
OPENAI_API_KEY for the voice pipeline).

When the cross-provider Gemini hop (gemini_chat_fallback.py) is ALSO exhausted
BEFORE any token has streamed, it hands off here. This runs the SAME multi-turn
tool loop on GPT and emits the SAME SSE event dicts (text_delta / tool_thinking /
clarification_ui / done / error), so the client sees a normal streamed reply from
a third, independent provider/billing account instead of a dead chat.

This is the LAST tier: unlike claude_client -> Gemini and Gemini -> here, there is
nowhere further to fall back to. Any failure here (before or after a token has
streamed) ends the turn with exactly one friendly `error` event -- never the raw
provider exception text, per chat_error_copy.py.

Same trade-off as the Gemini hop: a rare-outage path, so it favours "keep the
conversation alive" over feature parity. Inbound image/document attachments are
dropped (with a log line) rather than translated.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from ..config.settings import settings
from ..lib.logger import logger
from .analytics.llm_telemetry import openai_usage_tokens, start_llm_generation
from .chat_error_copy import CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE
from .tool_executor import ToolExecutor

_MAX_TURNS = 6
# Text the model emits before a tool call is brief narration ("let me check…").
# Anything longer is almost certainly the start of a final answer, not narration —
# the same heuristic claude_client and gemini_chat_fallback use.
_NARRATION_MAX_CHARS = 80

_client: Any = None


def _get_openai_client() -> Any:
    """Lazy singleton, mirrors ModelProvider._get_gemini_client's pattern. Raising
    loudly on a missing key (rather than constructing a client that will 401 on
    first use) is caught by this module's own try/except, same as any other
    failure here."""
    global _client
    if _client is None:
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not set — GPT chat fallback unavailable")
        from openai import AsyncOpenAI  # type: ignore

        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


def _flatten_system_prompt(system_prompt: str | list[dict[str, Any]]) -> str:
    """Anthropic `system` is a string OR a list of content blocks (some carrying
    cache_control). OpenAI wants ONE system message string, so concatenate the text."""
    if isinstance(system_prompt, str):
        return system_prompt
    parts: list[str] = []
    for block in system_prompt or []:
        if isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
        elif isinstance(block, str):
            parts.append(block)
    return "\n\n".join(parts)


def _block_field(block: Any, key: str) -> Any:
    """Read a field off an Anthropic content block that may be a plain dict (the ones we
    build) or an SDK object (the assistant turn stores raw TextBlock/ToolUseBlock objects
    when an Anthropic tool turn succeeded before the outage)."""
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _anthropic_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic tool defs ({name, description, input_schema}) into OpenAI's
    function-tool shape. Anthropic's input_schema is already a plain JSON schema, and
    OpenAI's default (non-strict) function calling accepts that directly, so no
    schema sanitiser is needed here either (same finding as the Gemini hop)."""
    result: list[dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return result


def _anthropic_messages_to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the Anthropic-format conversation into OpenAI chat messages.

    assistant -> one message carrying `content` and/or `tool_calls`; a user tool_result
    block -> its OWN separate {"role": "tool", "tool_call_id", "content"} message (OpenAI,
    unlike Gemini/Anthropic, requires tool responses as standalone top-level messages, one
    per tool_call_id, immediately after the assistant message that made the calls).
    OpenAI's function tool_call_id must match the matching assistant tool_calls[].id, which
    Anthropic only carries on the tool_use block, so we map tool_use_id -> name as we walk
    (needed for logging/consistency; the id itself is threaded straight through). Image/
    document blocks are dropped (logged) — see the module docstring.
    """
    openai_messages: list[dict[str, Any]] = []
    tool_use_ids: set[str] = set()
    dropped_attachments = 0

    for msg in messages:
        role = _block_field(msg, "role")
        content = _block_field(msg, "content")

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            if isinstance(content, str):
                if content:
                    text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    btype = _block_field(block, "type")
                    if btype == "text":
                        text = _block_field(block, "text")
                        if text:
                            text_parts.append(str(text))
                    elif btype == "tool_use":
                        tu_id = _block_field(block, "id")
                        name = _block_field(block, "name")
                        args = _block_field(block, "input")
                        if tu_id and name:
                            tool_use_ids.add(tu_id)
                            tool_calls.append({
                                "id": tu_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args if isinstance(args, dict) else {}),
                                },
                            })
            entry: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            openai_messages.append(entry)
            continue

        # user (or any non-assistant) role. A content list may interleave plain text
        # with tool_result blocks; tool_result must flush as its own message the
        # instant we hit one, so relative order survives the split.
        if isinstance(content, str):
            if content:
                openai_messages.append({"role": "user", "content": content})
        elif isinstance(content, list):
            pending_text: list[str] = []
            for block in content:
                btype = _block_field(block, "type")
                if btype == "text":
                    text = _block_field(block, "text")
                    if text:
                        pending_text.append(str(text))
                elif btype == "tool_result":
                    if pending_text:
                        openai_messages.append({"role": "user", "content": "".join(pending_text)})
                        pending_text = []
                    tu_id = _block_field(block, "tool_use_id") or ""
                    if tu_id in tool_use_ids:
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tu_id,
                            "content": str(_block_field(block, "content")),
                        })
                elif btype in ("image", "document"):
                    dropped_attachments += 1
                # unknown block types are skipped
            if pending_text:
                openai_messages.append({"role": "user", "content": "".join(pending_text)})

    if dropped_attachments:
        logger.warn("OpenAI fallback: dropped attachment blocks (not supported on fallback path)", {
            "count": dropped_attachments,
        })
    return openai_messages


async def stream_openai_chat_fallback(
    *,
    tool_executor: ToolExecutor,
    system_prompt: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    captured_tool_data: list[dict[str, Any]] | None = None,
    tool_names_used: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the chat turn on GPT and yield the same SSE events claude_client /
    gemini_chat_fallback do. Self-contained: owns its own multi-turn tool loop and
    always ends with one `done` event, or one friendly `error` event on total
    failure (this is the last tier, so unlike the Gemini hop it never delegates
    further). `captured_tool_data` / `tool_names_used` carry forward anything
    already produced by earlier Anthropic/Gemini turns before the handoff."""
    captured: list[dict[str, Any]] = list(captured_tool_data or [])
    names_used: list[str] = tool_names_used if tool_names_used is not None else []
    # One telemetry generation per streamed GPT turn; finish() is idempotent,
    # so the outer error handler can safely close the latest one on failure.
    recording: Any = None

    try:
        client = _get_openai_client()
        openai_tools = _anthropic_tools_to_openai(tools)
        openai_messages: list[dict[str, Any]] = [
            {"role": "system", "content": _flatten_system_prompt(system_prompt) or ""},
            *_anthropic_messages_to_openai_messages(messages),
        ]

        logger.info("OpenAI fallback: starting", {
            "model": settings.OPENAI_CHAT_MODEL,
            "messages_in_history": len(openai_messages),
            "tools": len(tools),
        })

        for _ in range(_MAX_TURNS):
            turn_text_buffer: list[str] = []
            buffered_chars = 0
            committed_to_streaming = False
            full_text_parts: list[str] = []
            # tool_call index -> accumulated {id, name, arguments}; OpenAI streams
            # each field incrementally across chunks, keyed by a per-turn index.
            tool_call_fragments: dict[int, dict[str, str]] = {}

            recording = start_llm_generation(
                model=settings.OPENAI_CHAT_MODEL, provider="openai", caller="chat_openai_fallback",
                uid=tool_executor.user_id,
            )
            turn_usage: Any = None
            stream = await client.chat.completions.create(
                model=settings.OPENAI_CHAT_MODEL,
                messages=openai_messages,
                tools=openai_tools or None,
                stream=True,
                # Asks OpenAI to append one final chunk carrying token usage
                # (choices empty on that chunk); without it usage never streams.
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    turn_usage = chunk_usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    full_text_parts.append(delta.content)
                    if committed_to_streaming:
                        yield {"type": "text_delta", "delta": delta.content}
                    else:
                        turn_text_buffer.append(delta.content)
                        buffered_chars += len(delta.content)
                        if buffered_chars >= _NARRATION_MAX_CHARS:
                            committed_to_streaming = True
                            for chunk_text in turn_text_buffer:
                                yield {"type": "text_delta", "delta": chunk_text}
                            turn_text_buffer.clear()
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        frag = tool_call_fragments.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            frag["id"] = tc.id
                        if tc.function and tc.function.name:
                            frag["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            frag["arguments"] += tc.function.arguments
            recording.finish(tokens=openai_usage_tokens(turn_usage))

            if not tool_call_fragments:
                # Final turn — flush any still-buffered narration as the answer.
                for chunk_text in turn_text_buffer:
                    yield {"type": "text_delta", "delta": chunk_text}
                turn_text_buffer.clear()
                break

            # Tool turn: any buffered text was narration that preceded the call.
            narration = "".join(turn_text_buffer).strip()
            if narration:
                yield {"type": "tool_thinking", "message": narration}
            turn_text_buffer.clear()

            assistant_text = "".join(full_text_parts).strip()
            ordered_calls = [tool_call_fragments[i] for i in sorted(tool_call_fragments)]
            openai_messages.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                    for call in ordered_calls
                ],
            })

            async def _run_tool(call: dict[str, str]) -> tuple[str, str, Any, Exception | None]:
                try:
                    args = json.loads(call["arguments"]) if call["arguments"] else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                    logger.warn("OpenAI fallback: tool call arguments were not valid JSON", {
                        "tool": call["name"],
                    })
                try:
                    result = await tool_executor.execute(call["name"], args)
                    return (call["id"], call["name"], result, None)
                except Exception as exc:
                    logger.exception("OpenAI fallback: tool error", {
                        "tool": call["name"], "error": str(exc),
                    })
                    return (call["id"], call["name"], None, exc)

            results = await asyncio.gather(*[_run_tool(call) for call in ordered_calls])

            # Clarification sentinel — same contract as the Anthropic/Gemini loops.
            clarification = next(
                (r for r in results if isinstance(r[2], dict) and r[2].get("__clarification__")),
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
                    (d["data"] for d in captured if d["tool"] == "set_reminder"), None
                )
                metadata: dict[str, Any] = {
                    "tool_names": names_used, "awaiting_clarification": True,
                }
                if reminder_data:
                    metadata["reminder"] = reminder_data
                yield {"type": "done", "metadata": metadata}
                return

            # Feed tool results back for the next turn.
            for tool_call_id, name, result, exc in results:
                names_used.append(name)
                if exc is not None:
                    payload: Any = {"error": str(exc)}
                elif isinstance(result, dict):
                    if name == "set_reminder" and "error" not in result:
                        captured.append({"tool": name, "data": result})
                    payload = result
                else:
                    payload = {"result": str(result)}
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(payload, default=str),
                })
        else:
            logger.warn("OpenAI fallback: max turns exceeded", {"tools_used": names_used})

        reminder_data = next((d["data"] for d in captured if d["tool"] == "set_reminder"), None)
        metadata = {"tool_names": names_used}
        if reminder_data:
            metadata["reminder"] = reminder_data
        yield {"type": "done", "metadata": metadata}

    except Exception as exc:
        if recording is not None:
            recording.finish(success=False, error_type=type(exc).__name__)
        # Last tier: nowhere further to fall back to. Log the real error, but the
        # client only ever sees the one friendly line.
        logger.exception("OpenAI fallback: failed", {
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        yield {"type": "error", "message": CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE}
