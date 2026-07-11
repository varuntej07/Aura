"""Per-session sonic-3 voice conditioning derived from the behavioral profile.

Maps the user's aura signals to Cartesia sonic-3 generation controls. A
profile-less user resolves to (None, None) so the default voice is constructed
byte-for-byte unchanged.
"""

from __future__ import annotations

# dominant_tone (communication style) -> speech cadence.
# Kept in a tight 0.90-0.95 band centered ~0.92: shifted down 0.05 from the
# original 0.95-1.00 band (2026-07-09) after the near-default band still read
# as too fast on a live call. Still close enough to sonic-3's natural default
# (1.0) that it stays unhurried rather than sluggish. The old 0.77-0.90 band
# before that dragged; going the other way (>=1.05) starts to sound rushed.
# sonic-3 treats speed as guidance, not a hard multiplier, and large shifts
# sound unnatural (valid range 0.6-2.0). Terse users (clipped, punchy) get the
# briskest end; verbose/formal get the most measured. speed MUST be a float
# for sonic-3 — the plugin raises ValueError on the string enum.
TONE_TO_SPEED: dict[str, float] = {
    "terse": 0.95,
    "playful": 0.93,
    "casual": 0.92,
    "verbose": 0.91,
    "formal": 0.90,
}

# emotional_state (user affect) -> sonic-3 emotion name. Positive affect is mirrored;
# negative affect is counterbalanced (a companion should soothe, not amplify distress).
# neutral and any unmapped state set no emotion.
#
# Values MUST be lowercase canonical names from
# emotion_tags.CARTESIA_SONIC3_EMOTION_NAMES: sonic-3's generation_config.emotion
# takes lowercase per the Cartesia API reference, and the livekit plugin passes
# the string through as-is (the old Capitalized TTSVoiceEmotion values were out
# of contract and likely silently ignored). Keys are the closed 7-value
# emotional_state vocabulary from user_aura_extractor.py — do not add keys the
# extractor can never produce.
EMOTIONAL_STATE_TO_VOICE_EMOTION: dict[str, str] = {
    "excited": "excited",            # mirror
    "curious": "curious",            # mirror
    "anticipatory": "anticipation",  # mirror
    "anxious": "calm",               # counterbalance; a primary emotion, best sonic-3 results
    "frustrated": "calm",            # counterbalance: frustration wants steadiness, not pity
    "sad": "sympathetic",            # counterbalanced warmth
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
