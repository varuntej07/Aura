"""Stop spoken side-effect language that the finalized turn did not authorize."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, Callable

from .capabilities import Capability

# Forward-order request to CREATE reminder work ("set a reminder", "remind you at 5").
# Blocked only when the finalized turn holds no reminder-write capability at all. Verb
# group uses `schedul\w*` so inflections ("scheduled", "scheduling") don't slip the gate.
_REMINDER_ACTION_LANGUAGE = re.compile(
    r"\b(?:set|create|schedul\w*|configure|add|make)\b.{0,40}\breminder\b|"
    r"\b(?:what|which|when)\b.{0,45}\breminder\b.{0,30}\b(?:fire|set|schedule)\b|"
    r"\bremind you\b",
    re.IGNORECASE,
)

# Language asserting a reminder was actually CREATED this turn, in any word order
# ("reminder set", "scheduled the reminder", "I'll remind you at 5"). Truthful only when
# set_reminder committed; otherwise the model invented a completed action (the reported
# reverse-order "reminder set" / "locked in" claim the old verb-first regex missed).
_REMINDER_SUCCESS_CLAIM = re.compile(
    r"\breminder\b.{0,25}\b(?:is\s+)?"
    r"(?:set|scheduled|saved|created|added|locked in|all set|ready|good to go)\b|"
    r"\b(?:set|scheduled|saved|created|added|locked in)\b.{0,25}\breminder\b|"
    r"\b(?:i(?:'|’)?ll|i\s+will|i(?:'|’)?ve|i\s+have)\b.{0,15}\bremind(?:ed)? you\b|"
    # Delivery-framed completion: asserts the reminder will ARRIVE, with no
    # set/scheduled/created verb ("you'll get the reminder", "reminder ... right
    # on time"). Reported live 2026-07-16: "You'll get the reminder for that
    # to-do task right on time" carried no success verb and slipped the gate.
    r"\byou(?:'|’)?ll\s+(?:get|receive|have)\b.{0,25}\breminder\b|"
    r"\breminder\b.{0,30}\b(?:right on time|on time)\b",
    re.IGNORECASE,
)

# Bare completion phrases with no object ("all set", "locked it in", "you're all set").
# A false claim only on a reminder turn that produced no receipt; harmless otherwise, so
# these are gated on reminder context to avoid over-blocking neutral confirmations.
_BARE_SUCCESS_CLAIM = re.compile(
    # "it|that" is optional so "locked in for 10 PM" matches, not just "locked it
    # in" (2026-07-16: "Locked in for 10 PM tonight" was the bare half of the claim).
    r"\b(?:all set|locked (?:it |that )?in|you(?:'|’)?re all set|"
    r"got (?:it|that) (?:set|scheduled|locked in)|it(?:'|’)?s (?:all )?set)\b",
    re.IGNORECASE,
)

# Reminder-turn-only: delivery/notification promises that assert the reminder will
# fire without ever naming a "reminder" ("it'll notify you", "you'll get a ping on
# phone and desktop"). Gated on reminder context so a neutral confirmation on some
# other turn ("you'll get a notification when your notes are ready") isn't caught.
_REMINDER_DELIVERY_CLAIM = re.compile(
    r"\byou(?:'|’)?ll\s+(?:get|receive|be)\b.{0,35}\b(?:notif\w+|ping|alert|nudge|reminded)\b|"
    r"\b(?:i|it)(?:'|’)?ll\s+(?:notify|ping|nudge|remind)\s+you\b|"
    r"\bnotif\w+\b.{0,30}\b(?:phone|desktop|device)\b",
    re.IGNORECASE,
)

# A question is a clarification, never a completed-action claim, even when it contains
# "set ... reminder" ("Which day should I set that reminder for?"). Detected per sentence
# so a declarative claim before a trailing question is still caught.
_INTERROGATIVE = re.compile(
    r"\?\s*$|"
    r"^(?:what|which|when|where|how|should|shall|do|does|did|can|could|would|"
    r"want|is there|are there)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT.split(text.strip()) if part]


def has_unauthorized_spoken_action(
    text: str,
    capabilities: frozenset[Capability],
    *,
    reminder_committed: bool = False,
) -> bool:
    """Return true when speech asserts reminder work the finalized turn cannot back up.

    Receipt-based: a "reminder set" / "locked in" success claim is honoured only when
    ``set_reminder`` actually committed this turn (``reminder_committed``). Holding the
    REMINDER_WRITE capability is NOT proof of a write - a turn can carry it while the tool
    is withheld for a clarification or is simply never called, and the model must not
    narrate success in that state (the reported "reminder set, no tool ran" bug). A
    forward-order request to invent reminder work is still blocked whenever the turn holds
    no reminder-write capability at all.
    """
    reminder_turn = Capability.REMINDER_WRITE in capabilities
    if not reminder_committed:
        for sentence in _sentences(text):
            if _INTERROGATIVE.search(sentence):
                continue  # a question is a clarification, not a completed-action claim
            if _REMINDER_SUCCESS_CLAIM.search(sentence):
                return True
            if reminder_turn and (
                _BARE_SUCCESS_CLAIM.search(sentence)
                or _REMINDER_DELIVERY_CLAIM.search(sentence)
            ):
                return True
    return bool(not reminder_turn and _REMINDER_ACTION_LANGUAGE.search(text))


def _chunk_text(item: object) -> str:
    if isinstance(item, str):
        return item
    delta = getattr(item, "delta", None)
    content = getattr(delta, "content", None)
    return content if isinstance(content, str) else ""


async def guard_spoken_action_stream(
    chunks: AsyncIterable[object],
    *,
    capabilities: frozenset[Capability],
    regenerate: Callable[[], AsyncIterable[object]] | None,
    neutral_recovery: str,
    on_blocked: Callable[[], None] | None = None,
    reminder_committed: Callable[[], bool] | None = None,
) -> AsyncIterable[object]:
    """Hold one sentence at a time, retry once, then fail closed with neutral speech.

    ``reminder_committed`` is polled at each flush (not snapshotted up front) so a success
    claim clears the instant ``set_reminder`` actually commits mid-generation, and stays
    blocked until it does.
    """
    pending: list[object] = []
    pending_text = ""

    def _blocked(text: str) -> bool:
        return has_unauthorized_spoken_action(
            text,
            capabilities,
            reminder_committed=bool(reminder_committed()) if reminder_committed else False,
        )

    async def _flush() -> AsyncIterable[object]:
        nonlocal pending, pending_text
        if _blocked(pending_text):
            if on_blocked is not None:
                on_blocked()
            pending = []
            pending_text = ""
            if regenerate is not None:
                async for retry_item in guard_spoken_action_stream(
                    regenerate(),
                    capabilities=capabilities,
                    regenerate=None,
                    neutral_recovery=neutral_recovery,
                    on_blocked=on_blocked,
                    reminder_committed=reminder_committed,
                ):
                    yield retry_item
            else:
                yield neutral_recovery
            return
        buffered = pending
        pending = []
        pending_text = ""
        for buffered_item in buffered:
            yield buffered_item

    async for item in chunks:
        text = _chunk_text(item)
        if not text:
            yield item
            continue
        pending.append(item)
        pending_text += text
        if _SENTENCE_END.search(pending_text) or len(pending_text) >= 320:
            blocked = _blocked(pending_text)
            async for flushed in _flush():
                yield flushed
            if blocked:
                return

    if pending:
        async for flushed in _flush():
            yield flushed
