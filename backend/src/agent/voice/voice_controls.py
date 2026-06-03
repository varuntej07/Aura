"""Per-session sonic-3 voice conditioning derived from the behavioral profile.

Maps the user's aura signals to Cartesia sonic-3 generation controls. A
profile-less user resolves to (None, None) so the default voice is constructed
byte-for-byte unchanged.
"""

from __future__ import annotations

# dominant_tone (communication style) -> speech cadence.
# Kept conservative (0.88-1.0) because sonic-3 treats speed as guidance, not a hard multiplier,
# and large shifts sound unnatural. speed MUST be a float for sonic-3 — the
# plugin raises ValueError on the string enum.
TONE_TO_SPEED: dict[str, float] = {
    "terse": 0.9,
    "playful": 0.82,
    "casual": 0.77,
    "formal": 0.8,
    "verbose": 0.8,
}

# emotional_state (user affect) -> Cartesia TTSVoiceEmotion. Positive affect is mirrored;
# negative affect is counterbalanced (a companion should soothe, not amplify distress).
# neutral and any unmapped state set no emotion.
EMOTIONAL_STATE_TO_VOICE_EMOTION: dict[str, str] = {
    "excited": "Excited",
    "curious": "Curious",
    "anticipatory": "Anticipation",
    "anxious": "Calm",
    "frustrated": "Calm",
    "sad": "Sympathetic",
}


def derive_voice_controls(
    dominant_tone: str, dominant_emotion: str
) -> tuple[float | None, str | None]:
    """Map aura signals to (speed, emotion) for the Cartesia sonic-3 TTS.

    speed is always a float or None (never the string enum) to satisfy sonic-3.
    Returns (None, None) when both signals are absent so a profile-less user gets
    byte-identical default-voice behavior.
    """
    speed = TONE_TO_SPEED.get((dominant_tone or "").strip().lower())
    emotion = EMOTIONAL_STATE_TO_VOICE_EMOTION.get((dominant_emotion or "").strip().lower())
    return speed, emotion
