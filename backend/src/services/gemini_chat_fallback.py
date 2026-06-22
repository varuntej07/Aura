"""
gemini_chat_fallback.py — cross-provider chat fallback for the text /chat stream.

When the Anthropic chain (Sonnet -> Haiku) is exhausted BEFORE any token has streamed
(a total Anthropic outage), claude_client.send_text_turn_stream hands the rest of the
conversation here. This runs the SAME multi-turn tool loop on Gemini Flash and emits the
SAME SSE event dicts (text_delta / tool_thinking / clarification_ui / done / error), so the
client sees a normal streamed reply from a different provider instead of a dead chat.

It is a last resort, reached only when both Anthropic models are down, so it favours
"keep the conversation alive" over feature parity: inbound image/document attachments are
dropped (with a log line) rather than translated, since an outage is a rare moment and the
goal here is a working text reply, not multimodal fidelity.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..config.settings import settings
from ..lib.logger import logger
from .model_provider import get_model_provider
from .tool_executor import ToolExecutor

_MAX_TURNS = 6
# Text the model emits before a function call is brief narration ("let me check…").
# Anything longer is almost certainly the start of a final answer, not narration —
# the same heuristic claude_client uses to split tool_thinking from text_delta.
_NARRATION_MAX_CHARS = 80


def _flatten_system_prompt(system_prompt: str | list[dict[str, Any]]) -> str:
    """Anthropic `system` is a string OR a list of content blocks (some carrying
    cache_control). Gemini wants ONE system_instruction string, so concatenate the text."""
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


def _anthropic_tools_to_gemini(tools: list[dict[str, Any]]) -> list:
    """Translate Anthropic tool defs ({name, description, input_schema}) into ONE Gemini
    Tool with function declarations. `parameters_json_schema` takes the raw JSON schema
    directly (google-genai converts it internally), so no hand-written schema sanitiser is
    needed. Any trailing cache_control key on an Anthropic tool is simply ignored."""
    from google.genai import types  # type: ignore

    decls = []
    for tool in tools:
        name = tool.get("name")
        if not name:
            continue
        decls.append(
            types.FunctionDeclaration(
                name=name,
                description=tool.get("description", ""),
                parameters_json_schema=tool.get("input_schema") or {"type": "object", "properties": {}},
            )
        )
    if not decls:
        return []
    return [types.Tool(function_declarations=decls)]


def _anthropic_messages_to_gemini_contents(messages: list[dict[str, Any]]) -> list:
    """Translate the Anthropic-format conversation into Gemini `contents`.

    assistant -> "model"; user -> "user". A text block -> Part.from_text; an assistant
    tool_use block -> Part.from_function_call; a user tool_result block ->
    Part.from_function_response. Gemini's function response needs the function NAME, which
    Anthropic only carries on the matching tool_use block, so we map tool_use_id -> name as
    we walk. Image/document blocks are dropped (logged) — see the module docstring.
    """
    from google.genai import types  # type: ignore

    contents = []
    tool_use_names: dict[str, str] = {}  # tool_use_id -> function name
    dropped_attachments = 0

    for msg in messages:
        gem_role = "model" if _block_field(msg, "role") == "assistant" else "user"
        content = _block_field(msg, "content")
        parts = []

        if isinstance(content, str):
            if content:
                parts.append(types.Part.from_text(text=content))
        elif isinstance(content, list):
            for block in content:
                btype = _block_field(block, "type")
                if btype == "text":
                    text = _block_field(block, "text")
                    if text:
                        parts.append(types.Part.from_text(text=str(text)))
                elif btype == "tool_use":
                    tu_id = _block_field(block, "id")
                    name = _block_field(block, "name")
                    args = _block_field(block, "input")
                    if tu_id and name:
                        tool_use_names[tu_id] = name
                    if name:
                        parts.append(
                            types.Part.from_function_call(
                                name=name, args=dict(args) if isinstance(args, dict) else {}
                            )
                        )
                elif btype == "tool_result":
                    name = tool_use_names.get(_block_field(block, "tool_use_id") or "", "")
                    if name:
                        parts.append(
                            types.Part.from_function_response(
                                name=name,
                                response={"result": str(_block_field(block, "content"))},
                            )
                        )
                elif btype in ("image", "document"):
                    dropped_attachments += 1
                # unknown block types are skipped

        if parts:
            contents.append(types.Content(role=gem_role, parts=parts))

    if dropped_attachments:
        logger.warn("Gemini fallback: dropped attachment blocks (not supported on fallback path)", {
            "count": dropped_attachments,
        })
    return contents


def _chunk_parts(chunk: Any) -> list:
    """Defensively pull the content parts off a streamed Gemini chunk."""
    candidates = getattr(chunk, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return (getattr(content, "parts", None) or []) if content else []


async def stream_gemini_chat_fallback(
    *,
    tool_executor: ToolExecutor,
    system_prompt: str | list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    captured_tool_data: list[dict[str, Any]] | None = None,
    tool_names_used: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the chat turn on Gemini Flash and yield the same SSE events claude_client does.
    Self-contained: owns its own multi-turn tool loop and always ends with one `done` event
    (or one `error` event on total failure). `captured_tool_data` / `tool_names_used` carry
    forward anything already produced by Anthropic turns before the handoff."""
    from google.genai import types  # type: ignore

    captured: list[dict[str, Any]] = list(captured_tool_data or [])
    names_used: list[str] = tool_names_used if tool_names_used is not None else []

    try:
        client = get_model_provider()._get_gemini_client()
        gemini_tools = _anthropic_tools_to_gemini(tools)
        contents = _anthropic_messages_to_gemini_contents(messages)
        config = types.GenerateContentConfig(
            system_instruction=_flatten_system_prompt(system_prompt) or None,
            tools=gemini_tools or None,
            # We drive the tool loop ourselves (declarations, not Python callables), so keep
            # the SDK's automatic function calling out of the way.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=settings.ANTHROPIC_MAX_TOKENS,
        )

        logger.info("Gemini fallback: starting", {
            "model": settings.TIER_CHEAP,
            "turns_in_history": len(contents),
            "tools": len(tools),
        })

        for _ in range(_MAX_TURNS):
            turn_text_buffer: list[str] = []
            buffered_chars = 0
            committed_to_streaming = False
            full_text_parts: list[str] = []
            function_calls: list[Any] = []

            stream = await client.aio.models.generate_content_stream(
                model=settings.TIER_CHEAP,
                contents=contents,
                config=config,
            )
            async for chunk in stream:
                for part in _chunk_parts(chunk):
                    txt = getattr(part, "text", None)
                    if txt:
                        full_text_parts.append(txt)
                        if committed_to_streaming:
                            yield {"type": "text_delta", "delta": txt}
                        else:
                            turn_text_buffer.append(txt)
                            buffered_chars += len(txt)
                            if buffered_chars >= _NARRATION_MAX_CHARS:
                                committed_to_streaming = True
                                for chunk_text in turn_text_buffer:
                                    yield {"type": "text_delta", "delta": chunk_text}
                                turn_text_buffer.clear()
                    fc = getattr(part, "function_call", None)
                    if fc is not None:
                        function_calls.append(fc)

            if not function_calls:
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

            # Record the model's turn (text + function calls) in the running contents.
            model_parts: list[Any] = []
            assistant_text = "".join(full_text_parts).strip()
            if assistant_text:
                model_parts.append(types.Part.from_text(text=assistant_text))
            for fc in function_calls:
                model_parts.append(
                    types.Part.from_function_call(name=fc.name, args=dict(fc.args or {}))
                )
            contents.append(types.Content(role="model", parts=model_parts))

            async def _run_tool(fc: Any) -> tuple[str, Any, Exception | None]:
                try:
                    result = await tool_executor.execute(fc.name, dict(fc.args or {}))
                    return (fc.name, result, None)
                except Exception as exc:  # surfaced to the model as an error result
                    logger.exception("Gemini fallback: tool error", {"tool": fc.name, "error": str(exc)})
                    return (fc.name, None, exc)

            results = await asyncio.gather(*[_run_tool(fc) for fc in function_calls])

            # Clarification sentinel — same contract as the Anthropic loop.
            clarification = next(
                (r for r in results if isinstance(r[1], dict) and r[1].get("__clarification__")),
                None,
            )
            if clarification:
                _, clar_data, _ = clarification
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
                metadata: dict[str, Any] = {"tool_names": names_used, "awaiting_clarification": True}
                if reminder_data:
                    metadata["reminder"] = reminder_data
                yield {"type": "done", "metadata": metadata}
                return

            # Feed tool results back for the next turn.
            response_parts: list[Any] = []
            for name, result, exc in results:
                names_used.append(name)
                if exc is not None:
                    payload: dict[str, Any] = {"error": str(exc)}
                elif isinstance(result, dict):
                    if name == "set_reminder" and "error" not in result:
                        captured.append({"tool": name, "data": result})
                    payload = result
                else:
                    payload = {"result": str(result)}
                response_parts.append(types.Part.from_function_response(name=name, response=payload))
            contents.append(types.Content(role="user", parts=response_parts))
        else:
            logger.warn("Gemini fallback: max turns exceeded", {"tools_used": names_used})

        reminder_data = next((d["data"] for d in captured if d["tool"] == "set_reminder"), None)
        metadata = {"tool_names": names_used}
        if reminder_data:
            metadata["reminder"] = reminder_data
        yield {"type": "done", "metadata": metadata}

    except Exception as exc:
        logger.exception("Gemini fallback: failed", {
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        yield {"type": "error", "message": str(exc)}
