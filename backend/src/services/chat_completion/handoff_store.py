"""Cross-lane continuity courier for desktop text chat.

One doc per conversation at ``users/{uid}/chat_handoff/{conversation_id}``, holding the
last few text exchanges so a voice session started from the same conversation can load
them before it greets. It is a courier, not a record: a short TTL reaps it, and the voice
worker reads it as one more fully-defaulted source in its pre-session fan-out.

Why the client supplies the turns rather than the server persisting them: ``chat_turns``
is deleted on the happy path (``turn_store.mark_client_complete``), so no server-side text
transcript exists, and ``/chat`` already reconstructs context from client-supplied
``history``. Keeping one definition of "the conversation so far" beats building a second
that can disagree with it. Everything below is clamped server-side regardless.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as fs

from ...lib.logger import logger
from ..firebase import admin_firestore

COLLECTION = "chat_handoff"

FIELD_TURNS = "turns"
FIELD_CREATED_AT = "created_at"
FIELD_EXPIRES_AT = "expires_at"

# How long the handoff lingers before native Firestore TTL reaps it.
# (Set a TTL policy on the chat_handoff collection-group field `expires_at` in GCP.)
# Short on purpose: this exists to bridge a text lane into the call the user is starting
# right now, not to become a second copy of their conversation history.
HANDOFF_TTL = timedelta(hours=1)

# Bounds mirror the voice compaction budget (SOFT_RETAINED_RAW_TURNS in
# agent/voice/context_compaction.py, the summary budget in shared/context_summary.py):
# SOFT_RETAINED_RAW_TURNS is 8, so 4 exchanges leave the live call room before the first
# compaction fires, and 1800 chars is 450 tokens under the shared chars/4 estimator,
# which is exactly MAX_SUMMARY_TOKENS. The handoff never costs a session more than one
# compaction summary. The desktop client applies the same numbers; these are the contract.
MAX_EXCHANGES = 4
MAX_CHARS_PER_TURN = 600
MAX_CHARS_TOTAL = 1_800

# Same shape main.py enforces on /voice/token's conversation_id, so a value that can reach
# the worker through participant metadata is exactly the set of values storable here.
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _ref(user_id: str, conversation_id: str) -> fs.DocumentReference:
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(COLLECTION)
        .document(conversation_id)
    )


def clamp_turns(turns: list[Any]) -> list[dict[str, str]]:
    """Keep only text user/assistant turns inside the budget above.

    Trims oldest-first and only on an exchange boundary, so the agent never reads a
    reply whose question was dropped.
    """
    cleaned: list[dict[str, str]] = []
    for item in turns:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        cleaned.append({"role": role, "content": content[:MAX_CHARS_PER_TURN]})

    bounded = cleaned[-MAX_EXCHANGES * 2 :]
    while bounded and sum(len(turn["content"]) for turn in bounded) > MAX_CHARS_TOTAL:
        bounded = bounded[2:] if bounded[0]["role"] == "user" else bounded[1:]
    return bounded


async def save_handoff(
    user_id: str,
    conversation_id: str,
    turns: list[Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Overwrite this conversation's handoff. Fail-open: never raises, returns False.

    Idempotent by design. Voice can be started twice in a row (the bridged leg then the
    cascade leg), and a repeat write of the same conversation is simply the newer digest.
    """
    if not user_id or not CONVERSATION_ID_RE.fullmatch(conversation_id):
        return False
    bounded = clamp_turns(turns)
    if not bounded:
        return False
    now = now or datetime.now(UTC)

    def _write() -> None:
        _ref(user_id, conversation_id).set({
            FIELD_TURNS: bounded,
            FIELD_CREATED_AT: now,
            FIELD_EXPIRES_AT: now + HANDOFF_TTL,
        })

    try:
        await asyncio.to_thread(_write)
        return True
    except Exception as exc:
        logger.warn("handoff_store: save failed (voice still starts without text context)", {
            "user_id": user_id, "conversation_id": conversation_id, "error": str(exc),
        })
        return False


def read_handoff_turns(user_id: str, conversation_id: str) -> list[dict[str, str]]:
    """Blocking read-and-consume of this conversation's turns. [] for anything unusable.

    Consuming is the point. The conversation id outlives a call (it resets only on sign
    out), so a doc left in place would be re-read on every later voice start until the
    TTL caught up, and the prompt says "just before this call they were typing to you".
    Twenty minutes and one finished call later that sentence is false, and the agent
    would treat stale text as immediate context. This is a courier, not a record: it is
    delivered once and then gone. A later text turn writes a fresh one.

    Expiry is still checked rather than trusted to the TTL sweep, since Firestore's
    reaper is best-effort and can lag hours.
    """
    if not user_id or not CONVERSATION_ID_RE.fullmatch(conversation_id):
        return []
    ref = _ref(user_id, conversation_id)
    snap = ref.get()
    if not snap.exists:
        return []
    data = snap.to_dict() or {}
    try:
        ref.delete()
    except Exception as exc:
        # Delivered but not consumed. Better a possible repeat than a lost handoff, so
        # the turns below are still returned.
        logger.warn("handoff_store: consume failed, digest may repeat until TTL", {
            "user_id": user_id, "conversation_id": conversation_id, "error": str(exc),
        })
    expires_at = data.get(FIELD_EXPIRES_AT)
    if isinstance(expires_at, datetime) and expires_at <= datetime.now(UTC):
        return []
    return clamp_turns(data.get(FIELD_TURNS) or [])
