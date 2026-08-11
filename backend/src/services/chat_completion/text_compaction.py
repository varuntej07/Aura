"""Server-owned context compaction for desktop text chat.

The text sibling of ``agent/voice/context_compaction.py``. Same retention shape,
same summary schema (``shared/context_summary.py``), same budget ceiling. What
differs is ownership, and that difference is the whole reason this module exists.

Voice is a stateful worker: the LiveKit agent holds ``chat_ctx`` in memory, so it
can measure and rewrite context in place. ``POST /chat`` is stateless HTTP where
the CLIENT supplies history, which is why the desktop client used to upload its
whole transcript every turn so the server could use a slice of it. Here the
server owns the older context instead: it keeps a bounded typed summary on the
conversation document, and the client only has to send the recent raw tail.

Flow, per turn::

    turn ends, assistant message stored
        │
        ├─ unsummarized messages older than the retained tail >= trigger?   (arithmetic
        │                                                                    only, no read)
        └─ yes ─► claim (transactional, leased)
                    └─ read that seq range ─► fold with prior summary ─► store + advance
                                                                          watermark

Never on the hot path: the caller fires this and does not await it, so a turn's
latency is unchanged whether or not compaction runs. Every failure path leaves
the conversation exactly as it was, so the worst case is that the next turn runs
on its raw tail alone, which is the pre-compaction behaviour.
"""

from __future__ import annotations

import json
from datetime import timedelta

from ...config.settings import settings
from ...lib.logger import logger
from ...prompts import TEXT_CONTEXT_COMPACTION_PROMPT
from ...shared.context_summary import empty_summary, is_effectively_empty, normalize_summary
from ..model_provider import get_model_provider
from .. import desktop_chat_store

TEXT_COMPACTOR_VERSION = "2026-08-06.1"

# Kept raw, never summarized. Mirrors SOFT_RETAINED_RAW_TURNS = 8 in the voice
# compactor, counted in messages because that is what `seq` counts: 8 exchanges.
#
# LOAD-BEARING INVARIANT: this must stay <= settings.CHAT_HISTORY_WINDOW, the raw
# tail the client actually sends. The summary covers everything up to the
# watermark and the client covers the newest window; if this value ever exceeded
# that window, messages between the two would be in NEITHER, and would vanish
# from the model's context without a single error anywhere. At 16 vs 30 the two
# ranges overlap, which costs a little duplication and is the safe direction.
# Checked at import time at the bottom of this module, so a bad CHAT_HISTORY_WINDOW
# override is noticed on boot rather than by a user losing context weeks later.
RETAINED_RAW_MESSAGES = 16

# How much has to have aged out of the retained tail before folding is worth a
# model call. Mirrors voice's SOFT_TURN_TRIGGER = 16 turns.
COMPACTION_TRIGGER_MESSAGES = 32

# One fold never reads more than this. A conversation that somehow accumulated a
# huge unsummarized backlog (compaction disabled, then re-enabled) would otherwise
# build a summarizer prompt with no upper bound. The remainder is not lost: the
# watermark advances by what was folded, so the next turn folds the next chunk.
MAX_FOLD_MESSAGES = 60

# Long enough that a slow model call is not lapped, short enough that a crashed
# compaction unblocks an active thread quickly.
COMPACTION_CLAIM_LEASE = timedelta(minutes=2)

# Per-message caps inside the fold prompt. The summarizer needs the shape of a
# turn, not its full text; an 8000-char message contributes its opening only.
_MAX_CHARS_PER_MESSAGE = 800

SUMMARY_PREFIX = "<conversation_summary>"
SUMMARY_SUFFIX = "</conversation_summary>"


def check_window_invariant(client_history_window: int) -> bool:
    """Warn loudly if the retained tail ever outgrows the window the client sends.

    Deliberately a log rather than a raise: a misconfigured window degrades
    context quality, and failing every chat turn over it would be a far worse
    outcome than answering with a gap. But it must never be silent, because the
    symptom (the model forgetting a stretch of the middle of a thread) looks
    exactly like a model problem and nothing like a configuration one.

    Returns whether the invariant holds, so a caller that wants to assert can.
    """
    if RETAINED_RAW_MESSAGES > client_history_window:
        logger.warn("text_compaction: retained tail exceeds client history window", {
            "retained_raw_messages": RETAINED_RAW_MESSAGES,
            "client_history_window": client_history_window,
            "impact": "messages between the summary watermark and the client tail "
                      "reach the model in neither form",
        })
        return False
    return True


def render_summary_block(summary_json: str) -> str:
    """The system-prompt block for a stored summary, or "" when there is none.

    Wrapped in its own tag rather than voice's ``<voice_session_summary>`` so the
    two are distinguishable inside a prompt, and labelled as background so the
    model does not mistake a summarized decision for something the user just said.
    ``_CONVERSATION_AUTHORITY`` in the base prompt already states that rule; this
    naming is what lets it apply.
    """
    if not summary_json:
        return ""
    return (
        "Earlier turns in this same conversation, already compacted. Background "
        "only: the user's latest message is still the task, and anything here is "
        "context you may rely on but must not treat as a fresh request.\n"
        f"{SUMMARY_PREFIX}{summary_json}{SUMMARY_SUFFIX}"
    )


def compaction_bounds(*, next_seq: int, summarized_through_seq: int) -> tuple[int, int] | None:
    """The ``(after_seq, through_seq)`` range to fold, or None if nothing is due.

    Pure arithmetic on two integers already present on the session document, so
    the trigger check costs no read at all. Only messages OLDER than the retained
    raw tail are eligible, which is what guarantees compaction can never race
    ahead and summarize a turn the client is still showing raw.
    """
    if next_seq <= 0:
        return None
    newest_seq = next_seq - 1
    # Everything at or below this has aged out of the raw tail.
    through_seq = newest_seq - RETAINED_RAW_MESSAGES
    if through_seq < 0:
        return None
    after_seq = summarized_through_seq
    pending = through_seq - after_seq
    if pending < COMPACTION_TRIGGER_MESSAGES:
        return None
    # Bound one fold. The watermark advances only by what was actually folded.
    if pending > MAX_FOLD_MESSAGES:
        through_seq = after_seq + MAX_FOLD_MESSAGES
    return after_seq, through_seq


def serialize_messages(messages: list[dict[str, object]]) -> str:
    """Render stored message documents as the fold prompt's TURN block.

    Failed assistant messages are labelled rather than dropped: "Buddy could not
    answer this" is itself context, and silently omitting it would let the model
    summarize a broken exchange as a successful one.
    """
    lines: list[str] = []
    for message in messages:
        role = str(message.get(desktop_chat_store.FIELD_ROLE) or "")
        text = str(message.get(desktop_chat_store.FIELD_TEXT) or "").strip()
        if not text:
            continue
        status = str(message.get(desktop_chat_store.FIELD_STATUS) or "")
        label = role.upper()
        if role == desktop_chat_store.ROLE_ASSISTANT and status == desktop_chat_store.STATUS_FAILED:
            label = "ASSISTANT (FAILED)"
        lines.append(f"{label}: {text[:_MAX_CHARS_PER_MESSAGE]}")
    return "\n".join(lines)


async def maybe_compact(user_id: str, conversation_id: str) -> bool:
    """Fold aged-out turns into the conversation's summary if any are due.

    Returns whether a summary was actually stored. Fire-and-forget by design: the
    caller must not await this on the request path. Fail-open throughout, and the
    claim is released on every failure so a transient model error does not hold
    the lease for its full duration on an active thread.
    """
    try:
        state = await desktop_chat_store.get_context_state(user_id, conversation_id)
        if not state:
            return False
        bounds = compaction_bounds(
            next_seq=int(state[desktop_chat_store.FIELD_NEXT_SEQ]),
            summarized_through_seq=int(state[desktop_chat_store.FIELD_SUMMARIZED_THROUGH_SEQ]),
        )
        if bounds is None:
            return False
        after_seq, through_seq = bounds

        if not await desktop_chat_store.claim_compaction(
            user_id, conversation_id, lease=COMPACTION_CLAIM_LEASE
        ):
            return False
    except Exception as exc:
        logger.warn("text_compaction: pre-claim failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        return False

    try:
        messages = await desktop_chat_store.list_messages_in_seq_range(
            user_id,
            conversation_id,
            after_seq=after_seq,
            through_seq=through_seq,
            limit=MAX_FOLD_MESSAGES,
        )
        serialized = serialize_messages(messages)
        if not serialized:
            # Nothing foldable in the range (every message empty, or already
            # deleted). Advance anyway so the same empty range is not re-read on
            # every subsequent turn forever.
            await desktop_chat_store.store_context_summary(
                user_id,
                conversation_id,
                summary=state[desktop_chat_store.FIELD_CONTEXT_SUMMARY] or "",
                through_seq=through_seq,
            )
            return False

        prior = state[desktop_chat_store.FIELD_CONTEXT_SUMMARY] or json.dumps(
            empty_summary(), ensure_ascii=False, separators=(",", ":")
        )
        raw = await get_model_provider().cheap(
            TEXT_CONTEXT_COMPACTION_PROMPT.format(prior_summary=prior, turns=serialized),
            temperature=0.0,
        )
        summary = normalize_summary(raw)
        if is_effectively_empty(summary):
            # normalize_summary is fail-soft, so a refusal, a truncated reply, or
            # any non-JSON output arrives here as a valid but empty summary.
            # Storing it would advance the watermark over real turns and delete
            # them from context for good, which is far worse than not compacting:
            # the turns stay raw, and the next turn tries again.
            logger.warn("text_compaction: empty summary rejected, not advancing", {
                "user_id": user_id,
                "folded_messages": len(messages),
                "text_compactor_version": TEXT_COMPACTOR_VERSION,
            })
            await desktop_chat_store.release_compaction_claim(user_id, conversation_id)
            return False
        stored = await desktop_chat_store.store_context_summary(
            user_id, conversation_id, summary=summary, through_seq=through_seq
        )
        if stored:
            logger.info("text_compaction: summary stored", {
                "user_id": user_id,
                "folded_messages": len(messages),
                "through_seq": through_seq,
                "text_compactor_version": TEXT_COMPACTOR_VERSION,
            })
        return stored
    except Exception as exc:
        logger.warn("text_compaction: fold failed", {
            "user_id": user_id, "error_type": type(exc).__name__,
        })
        await desktop_chat_store.release_compaction_claim(user_id, conversation_id)
        return False


# Boot-time guard for the invariant documented on RETAINED_RAW_MESSAGES. Runs on
# import so an env override of CHAT_HISTORY_WINDOW that would open a context gap
# shows up in the startup logs instead of as mysterious forgetting in production.
check_window_invariant(settings.CHAT_HISTORY_WINDOW)
