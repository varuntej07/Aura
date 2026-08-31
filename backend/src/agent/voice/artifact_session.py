"""Sticky state for the card currently on the user's screen.

This replaces per-turn lexical arming as the primary signal for "this turn is
about the card". The regexes in ``spoken_action_guard`` only have to recognize
the turn that OPENS a card, which is the easy case because the opening request
states the noun ("draft me a cold DM"). Everything after that is state.

Why the inversion matters, from the 2026-08-12 session that motivated it: the
guard armed on 3 turns and every one of them carded correctly, and it stayed
disarmed on 5 turns of which 4 recited the draft aloud. The failures were not
the model ignoring instructions. They were revision turns whose wording the
lexicon could not recognize:

* "Why don't you make it a bit longer" - the follow-up pattern was anchored to
  the start of the utterance, so a mid-sentence "make it" did not match.
* "Where is the greeting? Where is the hook?" - the question pattern only
  accepted "what is / which".
* "This is voice." - the STT fragment that actually drove the generation, after
  the request had been finalized as a separate earlier turn.

No lexicon fixes that last one, because the ideal keyword ("give me a draft")
WAS present, DID match, and was discarded at a turn boundary. So arming stops
being a property of the current sentence and becomes a property of the session:
while a card is open, the turn is about the card until something else clearly
takes over.

Pure state and predicates, no I/O, matching ``spoken_action_guard``'s shape.
The wiring lives in ``buddy_agent``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .capabilities import VOICE_TOOL_REGISTRY, Capability, ToolEffect

# The two capabilities that own a card. A turn that commits to either keeps the
# session alive.
ARTIFACT_CAPABILITIES = frozenset(
    {Capability.OUTBOUND_DRAFT, Capability.VISIBLE_ARTIFACT}
)

# Capabilities whose every tool is read-only. A lookup is not a topic change:
# "make it mention his recent posts" commits to WEB_READ while still being
# entirely about the card on screen, and closing the session there would leave
# the follow-up unarmed. Reads are therefore treated exactly like committing to
# nothing, so they age the session without closing it. Only a WRITE or PRESENT
# elsewhere counts as the user moving on.
#
# Derived from the registry rather than listed, so a new read tool cannot
# silently start closing card sessions by being forgotten here.
READ_ONLY_CAPABILITIES = frozenset(
    capability
    for capability in Capability
    if (
        tools := [
            item
            for item in VOICE_TOOL_REGISTRY.values()
            if item.capability is capability
        ]
    )
    and all(item.effect is ToolEffect.READ for item in tools)
)

# Turns of unrelated conversation before an open card is assumed abandoned.
# A capability change closes the session immediately, so this ceiling only
# catches drift that never commits to any tool at all. Six is deliberately
# generous: the motivating session updated its card at least every other turn,
# and closing early reintroduces exactly the bug this module exists to fix.
MAX_IDLE_TURNS = 6

_FIRST_ACKS = (
    "Done, it's on your screen.",
    "That's on your screen.",
    "There it is, take a look.",
)
_REVISION_ACKS = (
    "Updated on your screen.",
    "Tweaked it, take a look.",
    "Updated that for you.",
)


@dataclass
class ArtifactSession:
    """The card on screen, and whether this turn still belongs to it."""

    capability: Capability | None = None
    kind: str = ""
    title: str = ""
    body: str = ""
    revision: int = 0
    idle_turns: int = 0
    # Advanced on every spoken acknowledgement so consecutive cards do not get
    # the same line. Buddy repeating one canned sentence reads as a machine.
    _ack_index: int = field(default=0, repr=False)

    @property
    def is_open(self) -> bool:
        return self.capability is not None

    def open(
        self,
        *,
        capability: Capability,
        kind: str,
        title: str,
        body: str,
    ) -> bool:
        """Record a delivered card. Returns True when it revised the open one.

        The return value is what the caller acks on. Switching capability (a
        code snippet requested while a draft is open) is a NEW card, not a
        revision, and saying "Updated on your screen" for something the user is
        seeing for the first time is wrong.
        """
        if self.capability is capability and self.body:
            self.note_revision(body=body, kind=kind, title=title)
            return True
        self.capability = capability
        self.kind = kind
        self.title = title
        self.body = body
        self.revision = 1
        self.idle_turns = 0
        return False

    def note_revision(self, *, body: str, kind: str = "", title: str = "") -> None:
        """Record an edit to the card already on screen."""
        self.body = body
        if kind:
            self.kind = kind
        if title:
            self.title = title
        self.revision += 1
        self.idle_turns = 0

    def note_turn(self, committed_capability: Capability | None) -> str:
        """Advance session lifetime for one finalized turn; return a close reason.

        An empty return means the session stayed open. ``committed_capability``
        is ``selection.active_capability``, which is None for a turn that
        commits to no tool at all. None must NOT close the session: "where is
        the hook?" is exactly such a turn, and closing on it would restore the
        original bug.
        """
        if not self.is_open:
            return ""
        if committed_capability in ARTIFACT_CAPABILITIES:
            self.idle_turns = 0
            return ""
        if (
            committed_capability is not None
            and committed_capability not in READ_ONLY_CAPABILITIES
        ):
            reason = f"capability_changed:{committed_capability.value}"
            self.close()
            return reason
        # Falls through for: no capability at all, a read-only lookup, and
        # SPEECH_CHANNEL. None of the three is the user moving on, so none
        # closes the session, but all of them age it so a long tangent does
        # eventually let go of a card nobody is talking about any more.
        self.idle_turns += 1
        if self.idle_turns >= MAX_IDLE_TURNS:
            self.close()
            return "idle_turns_exhausted"
        return ""

    def close(self) -> None:
        self.capability = None
        self.kind = ""
        self.title = ""
        self.body = ""
        self.revision = 0
        self.idle_turns = 0

    def next_ack(self, *, is_revision: bool) -> str:
        """A short spoken line for a card that is already rendered.

        Deterministic by construction: this is what the user hears instead of a
        second LLM generation, so the model never gets another chance to recite
        the body, and the turn loses a full round trip.
        """
        lines = _REVISION_ACKS if is_revision else _FIRST_ACKS
        line = lines[self._ack_index % len(lines)]
        self._ack_index += 1
        return line
