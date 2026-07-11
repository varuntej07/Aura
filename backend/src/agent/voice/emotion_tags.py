"""Bracket audio cues -> Cartesia sonic-3 inline speech markup, for the TTS path.

The LLM colors how a reply SOUNDS with bracket cues in the exact grammar it
already uses for [laughter] (see the "How you sound" section of
voice_prompt.py): an emotion name like [excited], or a curated delivery cue
like [whisper]. Cartesia sonic-3 only understands emotion/speed/volume as
inline SSML-style tags in the transcript, so this module converts the cues
code-side; the LLM itself never writes angle-bracket markup.

Allowlist contract (everything else is stripped, so a hallucinated cue like
[soft laughter] dies here instead of reaching TTS as dead air):
  * [laughter]           -> kept verbatim, the one real Cartesia nonverbalism
  * a delivery cue       -> its compound speed/volume/emotion markup
  * one of the 54 canonical sonic-3 emotion names -> <emotion value="name"/>

The caption/record path never sees any of this: strip_nonverbal_cue_stream
(text_sanitizer.py) already removes every [letters] cue upstream of captions,
the recorder, and post-session summaries.

Scope/reset semantics: Cartesia documents no reset tag, and the livekit plugin
mints a fresh websocket context per reply, so a tag colors at most the rest of
the current reply and can never leak across turns. No reset markup is emitted.

Design rules (same as text_sanitizer.py): pure, deterministic, fail open —
any internal error returns the original text rather than dropping the turn.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

from .text_sanitizer import bracket_cue_holdback_index, strip_nonverbal_cues

# The 54 emotion values sonic-3 accepts (lowercase, per the Cartesia API
# reference). generation_config.emotion and <emotion value="..."/> share this
# vocabulary. test_voice_controls.py cross-checks it against the plugin's
# TTSVoiceEmotion literal to catch upstream renames.
CARTESIA_SONIC3_EMOTION_NAMES: frozenset[str] = frozenset(
    {
        "neutral", "happy", "excited", "enthusiastic", "elated", "euphoric",
        "triumphant", "amazed", "surprised", "flirtatious", "curious", "content",
        "peaceful", "serene", "calm", "grateful", "affectionate", "trust",
        "sympathetic", "anticipation", "mysterious", "angry", "mad", "outraged",
        "frustrated", "agitated", "threatened", "disgusted", "contempt", "envious",
        "sarcastic", "ironic", "sad", "dejected", "melancholic", "disappointed",
        "hurt", "guilty", "bored", "tired", "rejected", "nostalgic", "wistful",
        "apologetic", "hesitant", "insecure", "confused", "resigned", "anxious",
        "panicked", "alarmed", "scared", "proud", "confident", "distant",
        "skeptical", "contemplative", "determined",
    }
)

LAUGHTER_CUE_NAME = "laughter"

# Curated compound delivery cues. Ratios sit inside the documented sonic-3
# ranges (speed 0.6-1.5, volume 0.5-2.0). No reset markup follows: each reply
# is a fresh Cartesia context, so "colors the rest of this reply" is the
# intended semantic (a whispered aside stays whispered).
DELIVERY_CUE_TO_INLINE_SSML: dict[str, str] = {
    # Quiet, leaned-in intimacy: late-night check-ins, comfort, "between us".
    "whisper": '<volume ratio="0.6"/><speed ratio="0.9"/>',
    # The genuinely big "no way" celebration; an emotion tag alone stays at
    # normal loudness, so pair it with louder + slightly faster delivery.
    "hyped": '<volume ratio="1.3"/><speed ratio="1.1"/><emotion value="excited"/>',
}

# Same cue shape as text_sanitizer._NONVERBAL_CUE, but capturing the inner
# name. Letters + spaces only, so [POINT:640,360:save] and footnotes like [1]
# never match.
_BRACKET_CUE = re.compile(r"\[([A-Za-z][A-Za-z ]*)\]")

# The exact markup this module can emit, for the fallback strip. Bounded
# character classes keep it from ever eating across two tags or a stray '<'.
_INLINE_SPEECH_MARKUP = re.compile(
    r'<(?:emotion\s+value|speed\s+ratio|volume\s+ratio)="[^"<>]*"\s*/>'
)

_MULTISPACE = re.compile(r"[ \t]{2,}")


def _replace_bracket_cue(match: re.Match) -> str:
    cue_name = match.group(1).strip().lower()
    if cue_name == LAUGHTER_CUE_NAME:
        return match.group(0)  # verbatim: Cartesia actually laughs at this one
    if cue_name in DELIVERY_CUE_TO_INLINE_SSML:
        return DELIVERY_CUE_TO_INLINE_SSML[cue_name]
    if cue_name in CARTESIA_SONIC3_EMOTION_NAMES:
        return f'<emotion value="{cue_name}"/>'
    return ""  # hallucinated cue ([soft laughter], [chuckle], ...): never reaches TTS


def convert_audio_cues_for_sonic3(text: str) -> str:
    """Convert allowlisted bracket cues to sonic-3 inline markup, strip the rest.

    Pure and deterministic. Case-insensitive on the cue name. Does NOT strip
    the whole string: mid-stream chunks must keep their boundary spaces.
    Returns the original text unchanged on any internal error.
    """
    if not text:
        return text
    try:
        s = _BRACKET_CUE.sub(_replace_bracket_cue, text)
        return _MULTISPACE.sub(" ", s)
    except Exception:
        return text


def strip_inline_speech_markup(text: str) -> str:
    """Remove sonic-3-only speech markup for a TTS engine that would read it aloud.

    Strips the emotion/speed/volume tags this module emits AND every bracket
    cue (including [laughter], which only sonic-3 voices). Used by the
    fallback TTS wrapper, which receives whole sentences, so a trailing strip
    is safe here. Fail-open like everything else on this path.
    """
    if not text:
        return text
    try:
        s = _INLINE_SPEECH_MARKUP.sub("", text)
        s = strip_nonverbal_cues(s)
        return _MULTISPACE.sub(" ", s).strip()
    except Exception:
        return text


async def convert_audio_cue_stream(
    text_stream: AsyncIterable[str],
) -> AsyncIterator[str]:
    """Streaming convert_audio_cues_for_sonic3 with cross-chunk cue holdback.

    A cue split as "[exci" + "ted]" must still convert, so a trailing unclosed
    "[" whose tail could still grow into a cue is held back until more text
    arrives (shared bracket_cue_holdback_index, same grammar as the caption
    strip). Emitted markup contains no "[", so every yielded chunk carries only
    complete tags — and the default tts_node pushes chunks verbatim, so a tag
    can never be split downstream. Fail-open: an unterminated "[..." at stream
    end was never a cue and is emitted as-is.
    """
    pending = ""
    async for chunk in text_stream:
        if not isinstance(chunk, str):
            if pending:
                yield pending
                pending = ""
            yield chunk
            continue
        pending = convert_audio_cues_for_sonic3(pending + chunk)
        cut = bracket_cue_holdback_index(pending)
        emit, pending = pending[:cut], pending[cut:]
        if emit:
            yield emit
    if pending:
        yield pending
