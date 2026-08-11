"""Surface-neutral conversation summary schema and budget enforcement.

Lifted out of `agent/voice/context_compaction.py` so the text chat path can reuse
the same summary shape rather than growing a second, subtly different one. Only
the parts with no LiveKit dependency live here: the field schema, the empty
skeleton, the normalizer, and the token estimator. Turn grouping, serialization,
and the compactor itself stay surface-specific, because what counts as "a turn"
differs between a voice session and an HTTP transcript.

The tag that wraps a rendered summary is deliberately NOT here: voice emits
`<voice_session_summary>` and text emits `<conversation_summary>`, and a shared
tag would make the two indistinguishable inside a prompt.
"""

from __future__ import annotations

import json
import math
import re

MAX_SUMMARY_TOKENS = 450

SUMMARY_FIELDS = (
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
LIST_FIELDS = frozenset(
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


def estimate_tokens(characters: int) -> int:
    """Conservative dependency-free estimate. Deliberately crude: it is used to
    decide when to compact, never to decide what a provider will charge."""
    return math.ceil(characters / 4)


def is_effectively_empty(summary_json: str) -> bool:
    """Whether a normalized summary carries no information at all.

    ``normalize_summary`` is fail-soft: any output the model produces that is not
    valid JSON in the expected shape comes back as the empty skeleton rather than
    raising. That is the right behaviour for rendering, but a caller that is about
    to ADVANCE A WATERMARK on the strength of a summary needs to tell "the model
    summarized this to nothing" apart from "the model failed", because storing an
    empty summary over real turns discards them permanently.
    """
    try:
        parsed = json.loads(summary_json)
    except (TypeError, json.JSONDecodeError):
        return True
    if not isinstance(parsed, dict):
        return True
    return not any(parsed.get(field) for field in SUMMARY_FIELDS)


def empty_summary() -> dict[str, str | list[str]]:
    return {
        field: ([] if field in LIST_FIELDS else "")
        for field in SUMMARY_FIELDS
    }


def normalize_summary(raw: str) -> str:
    """Coerce a model's summary reply into the fixed schema, under budget.

    Fail-soft by construction: unparseable output yields an empty summary rather
    than raising, because a missing summary degrades a turn while an exception
    would fail it. The trailing loop is what guarantees the summary can never
    grow without bound across repeated compactions - it evicts from the longest
    list field first, and only falls back to truncating free text once every list
    is exhausted.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    normalized = empty_summary()
    if isinstance(parsed, dict):
        for field in SUMMARY_FIELDS:
            value = parsed.get(field)
            if field in LIST_FIELDS:
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

    while estimate_tokens(len(_dump())) > MAX_SUMMARY_TOKENS:
        longest_list = max(
            LIST_FIELDS,
            key=lambda key: sum(len(str(value)) for value in normalized[key]),
        )
        values = normalized[longest_list]
        if isinstance(values, list) and values:
            values.pop()
            continue
        longest_text = max(
            (field for field in SUMMARY_FIELDS if field not in LIST_FIELDS),
            key=lambda key: len(str(normalized[key])),
        )
        text = str(normalized[longest_text])
        if not text:
            break
        normalized[longest_text] = text[: max(0, len(text) - 80)]
    return _dump()
