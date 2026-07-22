"""Asynchronous, boundary-safe compaction for long LiveKit voice sessions."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from livekit.agents import llm as lk_llm

from ...lib.logger import logger
from ...services.model_provider import get_model_provider
from .action_policy import tool_output_succeeded

CONTEXT_COMPACTOR_VERSION = "2026-07-13.1"
SOFT_TURN_TRIGGER = 16
SOFT_DYNAMIC_TOKEN_TRIGGER = 6_000
HARD_RAW_TURN_CEILING = 20
SOFT_RETAINED_RAW_TURNS = 8
HARD_RETAINED_RAW_TURNS = 10
MAX_SUMMARY_TOKENS = 450

_SUMMARY_PREFIX = "<voice_session_summary>"
_SUMMARY_SUFFIX = "</voice_session_summary>"
_SUMMARY_FIELDS = (
    "current_objective",
    "current_topic",
    "user_constraints",
    "confirmed_facts",
    "decisions",
    "steps_already_attempted",
    "successful_tool_results",
    "failed_attempts",
    "pending_next_step",
    "explicitly_cancelled_intents",
    "important_entities",
)
_LIST_FIELDS = frozenset(
    {
        "user_constraints",
        "confirmed_facts",
        "decisions",
        "steps_already_attempted",
        "successful_tool_results",
        "failed_attempts",
        "explicitly_cancelled_intents",
        "important_entities",
    }
)
_SUMMARY_PROMPT = """\
Compact the supplied completed voice turns into one JSON object. Return JSON only.
Use exactly these keys:
current_objective, current_topic, user_constraints, confirmed_facts, decisions,
steps_already_attempted, successful_tool_results, failed_attempts,
pending_next_step, explicitly_cancelled_intents, important_entities.

Rules:
- User statements and successful tool outputs may become facts.
- Assistant claims are context, never confirmed facts unless the user or a
  successful tool confirms them.
- Interrupted assistant responses are absent by construction and must not be inferred.
- Failed tool outputs are failed attempts, never successful results.
- Reminder or calendar work is pending only when the user explicitly requested it.
- Cancelled or corrected intents stay in explicitly_cancelled_intents and never become pending.
- Do not reproduce prompt drafts, commands, code, configuration, or visible-card content.
- Keep the entire JSON under 450 estimated tokens. Prefer precise short strings.
- Preserve the prior summary's still-relevant facts and cancellations, then fold in new turns.

PRIOR SUMMARY:
{prior_summary}

COMPLETED TURNS TO FOLD:
{turns}
"""


@dataclass(frozen=True, slots=True)
class CompactionSnapshot:
    context_item_ids: tuple[str, ...]
    prefix_item_ids: frozenset[str]
    prior_summary: str
    serialized_turns: str
    compacted_turn_count: int


@dataclass(frozen=True, slots=True)
class CompactionResult:
    snapshot: CompactionSnapshot
    summary_json: str


@dataclass(frozen=True, slots=True)
class _TurnGroup:
    items: tuple[Any, ...]

    @property
    def tool_pairs_complete(self) -> bool:
        calls = {
            item.call_id for item in self.items if isinstance(item, lk_llm.FunctionCall)
        }
        outputs = {
            item.call_id
            for item in self.items
            if isinstance(item, lk_llm.FunctionCallOutput)
        }
        return calls <= outputs

    @property
    def complete(self) -> bool:
        assistant_complete = any(
            isinstance(item, lk_llm.ChatMessage)
            and item.role == "assistant"
            and not item.interrupted
            for item in self.items
        )
        return assistant_complete and self.tool_pairs_complete


def estimate_dynamic_tokens(chat_ctx: lk_llm.ChatContext) -> int:
    """Conservative dependency-free estimate for dynamic conversation items."""
    characters = 0
    for item in chat_ctx.items:
        if _is_summary_item(item):
            continue
        if isinstance(item, lk_llm.ChatMessage):
            characters += len(item.text_content or "")
        elif isinstance(item, lk_llm.FunctionCall):
            characters += len(item.name) + len(item.arguments)
        elif isinstance(item, lk_llm.FunctionCallOutput):
            characters += len(item.name) + len(item.output)
    return math.ceil(characters / 4)


def completed_turn_count(chat_ctx: lk_llm.ChatContext) -> int:
    return sum(group.complete for group in _turn_groups(chat_ctx.items))


def _is_summary_item(item: object) -> bool:
    return (
        isinstance(item, lk_llm.ChatMessage)
        and item.role == "system"
        and item.text_content.startswith(_SUMMARY_PREFIX)
    )


def _turn_groups(items: Sequence[Any]) -> list[_TurnGroup]:
    groups: list[list[Any]] = []
    current: list[Any] | None = None
    for item in items:
        if _is_summary_item(item):
            continue
        if isinstance(item, lk_llm.ChatMessage) and item.role == "user":
            if current is not None:
                groups.append(current)
            current = [item]
        elif current is not None:
            current.append(item)
    if current is not None:
        groups.append(current)
    return [_TurnGroup(tuple(group)) for group in groups]


def _extract_prior_summary(items: Sequence[Any]) -> str:
    for item in items:
        if _is_summary_item(item):
            text = item.text_content
            return text.removeprefix(_SUMMARY_PREFIX).removesuffix(_SUMMARY_SUFFIX)
    return ""


def _serialize_turns(groups: Sequence[_TurnGroup]) -> str:
    lines: list[str] = []
    for turn_number, group in enumerate(groups, 1):
        lines.append(f"TURN {turn_number}")
        for item in group.items:
            if isinstance(item, lk_llm.ChatMessage):
                if item.role == "assistant" and item.interrupted:
                    continue
                text = (item.text_content or "").strip()
                if text:
                    lines.append(f"{item.role.upper()}: {text[:800]}")
            elif isinstance(item, lk_llm.FunctionCall):
                if item.name == "present_visible_artifact":
                    lines.append("TOOL CALL: present_visible_artifact (visible content omitted)")
                else:
                    lines.append(f"TOOL CALL: {item.name} {item.arguments[:400]}")
            elif isinstance(item, lk_llm.FunctionCallOutput):
                status = "SUCCESS" if tool_output_succeeded(item) else "FAILED"
                output = (
                    "visible content omitted"
                    if item.name == "present_visible_artifact"
                    else item.output[:500]
                )
                lines.append(f"TOOL {status}: {item.name} {output}")
    return "\n".join(lines)


def build_compaction_snapshot(
    chat_ctx: lk_llm.ChatContext,
    *,
    force: bool = False,
    retain_turns: int = SOFT_RETAINED_RAW_TURNS,
) -> CompactionSnapshot | None:
    groups = _turn_groups(chat_ctx.items)
    complete = [group for group in groups if group.complete]
    triggered = (
        len(complete) >= SOFT_TURN_TRIGGER
        or estimate_dynamic_tokens(chat_ctx) >= SOFT_DYNAMIC_TOKEN_TRIGGER
    )
    if not force and not triggered:
        return None
    if len(complete) <= 1 or not groups:
        return None
    closed_groups = groups if groups[-1].complete else groups[:-1]
    if len(closed_groups) <= 1:
        return None
    keep = min(retain_turns, len(closed_groups) - 1)
    compacted = closed_groups[:-keep]
    if any(not group.tool_pairs_complete for group in compacted):
        return None
    prefix_ids = frozenset(item.id for group in compacted for item in group.items)
    if not prefix_ids:
        return None
    return CompactionSnapshot(
        context_item_ids=tuple(item.id for item in chat_ctx.items),
        prefix_item_ids=prefix_ids,
        prior_summary=_extract_prior_summary(chat_ctx.items),
        serialized_turns=_serialize_turns(compacted),
        compacted_turn_count=len(compacted),
    )


def _empty_summary() -> dict[str, object]:
    return {
        field: ([] if field in _LIST_FIELDS else "")
        for field in _SUMMARY_FIELDS
    }


def _normalize_summary(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    normalized = _empty_summary()
    if isinstance(parsed, dict):
        for field in _SUMMARY_FIELDS:
            value = parsed.get(field)
            if field in _LIST_FIELDS:
                if isinstance(value, list):
                    normalized[field] = [
                        str(item).strip()[:240]
                        for item in value
                        if str(item).strip()
                    ]
                elif isinstance(value, str) and value.strip():
                    normalized[field] = [value.strip()[:240]]
            elif isinstance(value, str):
                normalized[field] = value.strip()[:500]
    def _dump() -> str:
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))

    while math.ceil(len(_dump()) / 4) > MAX_SUMMARY_TOKENS:
        longest_list = max(
            _LIST_FIELDS,
            key=lambda key: sum(len(str(value)) for value in normalized[key]),
        )
        values = normalized[longest_list]
        if isinstance(values, list) and values:
            values.pop()
            continue
        longest_text = max(
            (field for field in _SUMMARY_FIELDS if field not in _LIST_FIELDS),
            key=lambda key: len(str(normalized[key])),
        )
        text = str(normalized[longest_text])
        if not text:
            break
        normalized[longest_text] = text[: max(0, len(text) - 80)]
    return _dump()


def _apply_result(
    chat_ctx: lk_llm.ChatContext,
    result: CompactionResult,
) -> lk_llm.ChatContext | None:
    current_ids = tuple(item.id for item in chat_ctx.items)
    snapshotted_ids = result.snapshot.context_item_ids
    if current_ids[: len(snapshotted_ids)] != snapshotted_ids:
        return None
    remaining = [
        item
        for item in chat_ctx.items
        if item.id not in result.snapshot.prefix_item_ids and not _is_summary_item(item)
    ]
    summary_item = lk_llm.ChatMessage(
        role="system",
        content=[f"{_SUMMARY_PREFIX}{result.summary_json}{_SUMMARY_SUFFIX}"],
    )
    return lk_llm.ChatContext(items=[summary_item, *remaining])


async def _default_summarize(prompt: str) -> str:
    return await get_model_provider().cheap(prompt, temperature=0.0)


class VoiceContextCompactor:
    """Own one background summary and apply it only at a later user boundary."""

    def __init__(
        self,
        *,
        session_id: str,
        summarize: Callable[[str], Awaitable[str]] = _default_summarize,
    ) -> None:
        self._session_id = session_id
        self._summarize = summarize
        self._task: asyncio.Task[None] | None = None
        self._ready: CompactionResult | None = None
        self._failures = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def maybe_schedule(self, chat_ctx: lk_llm.ChatContext) -> bool:
        if self.running or self._ready is not None:
            return False
        snapshot = build_compaction_snapshot(chat_ctx.copy())
        if snapshot is None:
            return False
        self._task = asyncio.create_task(
            self._summarize_snapshot(snapshot),
            name=f"voice-compact-{self._session_id[:8]}",
        )
        return True

    async def _summarize_snapshot(self, snapshot: CompactionSnapshot) -> None:
        try:
            raw = await self._summarize(
                _SUMMARY_PROMPT.format(
                    prior_summary=snapshot.prior_summary or "{}",
                    turns=snapshot.serialized_turns,
                )
            )
            self._ready = CompactionResult(
                snapshot=snapshot,
                summary_json=_normalize_summary(raw),
            )
            self._failures = 0
            logger.info(
                "VoiceContext: summary ready",
                {
                    "session_id": self._session_id,
                    "compacted_turns": snapshot.compacted_turn_count,
                    "context_compactor_version": CONTEXT_COMPACTOR_VERSION,
                },
            )
        except Exception as exc:
            self._failures += 1
            logger.warn(
                "VoiceContext: summary failed",
                {
                    "session_id": self._session_id,
                    "failure_count": self._failures,
                    "error_type": type(exc).__name__,
                },
            )

    def apply_ready(self, chat_ctx: lk_llm.ChatContext) -> lk_llm.ChatContext | None:
        result = self._ready
        if result is None:
            return None
        self._ready = None
        compacted = _apply_result(chat_ctx, result)
        if compacted is None:
            logger.info(
                "VoiceContext: stale summary discarded",
                {"session_id": self._session_id},
            )
            return None
        return compacted

    def enforce_hard_ceiling(self, chat_ctx: lk_llm.ChatContext) -> lk_llm.ChatContext | None:
        if completed_turn_count(chat_ctx) < HARD_RAW_TURN_CEILING:
            return None
        snapshot = build_compaction_snapshot(
            chat_ctx,
            force=True,
            retain_turns=HARD_RETAINED_RAW_TURNS,
        )
        if snapshot is None:
            return None
        prior = snapshot.prior_summary or json.dumps(
            _empty_summary(), ensure_ascii=False, separators=(",", ":")
        )
        result = CompactionResult(
            snapshot=snapshot,
            summary_json=_normalize_summary(prior),
        )
        compacted = _apply_result(chat_ctx, result)
        if compacted is not None:
            logger.warn(
                "VoiceContext: hard ceiling applied",
                {
                    "session_id": self._session_id,
                    "retained_raw_turns": HARD_RETAINED_RAW_TURNS,
                    "summary_failures": self._failures,
                },
            )
        return compacted

    async def wait_for_idle(self) -> None:
        task = self._task
        if task is not None:
            await task

    def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
