"""Keep Buddy's speaking language matched to the language the user is speaking.

The failure this exists for: STT was switched to Deepgram's `multi` code-switching
mode, so the pipeline finally HEARS Spanish, Hindi, French and seven other
languages correctly. Nothing carried that through to TTS. build_tts_pipeline()
never passed `language`, so the Cartesia plugin default (`"en"`) applied to every
session, and a Spanish reply was synthesized with English pronunciation rules.
Hearing the user correctly and then mispronouncing the answer back at them is not
an improvement on answering in the wrong language; it is a different bug.

Nothing here needs a stored preference or a settings screen. Deepgram already
reports which language it heard, per utterance, and LiveKit already surfaces that
on the session event, so the correct language is knowable at the moment it
matters and the user is never asked a question the system can answer itself.

Cost of a switch: nothing measurable. cartesia.TTS.update_options() only mutates
the options object. The plugin rebuilds the request payload per synthesis
(_to_cartesia_options) and its ConnectionPool holds bare websockets that are not
bound to those options, so a change lands on the next utterance over the socket
that is already open. No reconnect, no extra round trip, nothing added to the
turn's critical path.

The Deepgram TTS leg is deliberately left alone. Every voice in voice_catalog.py
is an English Aura-2 model and Aura-2 has no counterpart in these languages. That
leg is only reached in a genuine Cartesia outage, where an English-sounding Buddy
still talking beats a correct-sounding Buddy that has gone silent.
"""

from __future__ import annotations

from typing import Any, Protocol

from ...lib.logger import logger


class _SupportsLanguageUpdate(Protocol):
    """The one method this module needs from a TTS leg."""

    def update_options(self, *, language: str) -> None: ...


# Every language Cartesia sonic-3.5 and sonic-3 can speak. Both legs of the TTS
# FallbackAdapter run one of those two models and their language lists are
# identical, so one set covers the pipeline.
#
# Source: Cartesia's own model docs, not inference from the voice names. The
# voices are labelled with English locales (en-US, en-GB) and it would be an easy
# and wrong assumption that the model is therefore English-only. It speaks 42
# languages, Telugu and Tamil among them.
#
# Kept as data rather than a call to the provider because it changes on model
# releases, not at runtime. Revisit when the pinned Cartesia model changes.
CARTESIA_LANGUAGES: frozenset[str] = frozenset(
    {
        "ar", "bg", "bn", "cs", "da", "de", "el", "en", "es", "fi",
        "fr", "gu", "he", "hi", "hr", "hu", "id", "it", "ja", "ka",
        "kn", "ko", "ml", "mr", "ms", "nl", "no", "pa", "pl", "pt",
        "ro", "ru", "sk", "sv", "ta", "te", "th", "tl", "tr", "uk",
        "vi", "zh",
    }
)

DEFAULT_LANGUAGE = "en"


def normalize(code: str | None) -> str:
    """Reduce a BCP-47 tag to the primary subtag Cartesia expects.

    Deepgram reports tags like "en", "es" and can report regional forms such as
    "es-419" or "en-IN"; Cartesia takes the bare language. Returns "" for
    anything unusable so callers have a single falsy case to check.

    This translates one provider's vocabulary into another's on a structured
    language tag. It is not a judgement about what the user meant from the words
    they said, so it is not the kind of string matching the repo bans.
    """
    if not code:
        return ""
    return str(code).strip().lower().split("-")[0]


class SpokenLanguageFollower:
    """Points the Cartesia legs at whichever language the user is actually speaking.

    Holds the legs rather than reaching into the session so that the wiring is
    visible at the call site and this stays testable against plain objects.
    """

    def __init__(
        self,
        *,
        cartesia_legs: list[_SupportsLanguageUpdate],
        session_id: str,
        user_id: str,
        initial_language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self._legs = cartesia_legs
        self._session_id = session_id
        self._user_id = user_id
        self._language = initial_language

    @property
    def language(self) -> str:
        """The language the TTS legs are currently set to."""
        return self._language

    def note_transcript(self, ev: Any) -> None:
        """Session handler for `user_input_transcribed`. Never raises.

        A fault in a convenience feature must not take down a live call, which is
        the same posture input_liveness.py takes for the same reason.
        """
        try:
            self._follow(ev)
        except Exception as exc:
            logger.warn(
                "VoiceSession: spoken language follow failed",
                {"session_id": self._session_id, "user_id": self._user_id, "error": str(exc)},
            )

    def _follow(self, ev: Any) -> None:
        # Interim transcripts get revised, and their language can be revised with
        # them. Only a final has settled enough to act on.
        if not getattr(ev, "is_final", False):
            return

        detected = normalize(getattr(ev, "language", None))
        if not detected or detected == self._language:
            # Unchanged is the common case and must cost nothing.
            return

        if detected not in CARTESIA_LANGUAGES:
            # Hold the current language rather than resetting to English. Deepgram
            # can transcribe languages Cartesia cannot speak (Persian is the live
            # example), and in that case the least-bad outcome is the voice we
            # already had, not a silent snap back to a default.
            logger.info(
                "VoiceSession: detected language not speakable, keeping current",
                {
                    "session_id": self._session_id,
                    "user_id": self._user_id,
                    "detected": detected,
                    "current": self._language,
                },
            )
            return

        for leg in self._legs:
            leg.update_options(language=detected)
        previous, self._language = self._language, detected

        # Logged on every switch, at info, on purpose. Whether a single
        # mis-detected short utterance ("Yeah.") can flip the language is a real
        # question that only production traffic answers. Debouncing before seeing
        # that would be guessing at a threshold.
        logger.info(
            "VoiceSession: spoken language switched",
            {
                "session_id": self._session_id,
                "user_id": self._user_id,
                "from": previous,
                "to": detected,
            },
        )
