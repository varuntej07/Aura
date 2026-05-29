"""
Tests for Task 7 — per-session sonic-3 voice conditioning in voice_agent.py.

Two units under test:
  * _derive_voice_controls  — pure tone/emotion -> (speed, emotion) mapping.
  * _fetch_user_aura_profile — reshapes the single UserAura/{uid} read into
                               {summary, dominant_tone, dominant_emotion}.

The invariant tests are the load-bearing ones: they guarantee a typo in a
mapping value can never ship a speed the sonic-3 plugin rejects (must be float)
or an emotion Cartesia doesn't recognize (must be a real TTSVoiceEmotion).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from livekit.plugins.cartesia.tts import TTSVoiceEmotion
import typing

from src.agent.voice_agent import (
    _EMOTIONAL_STATE_TO_VOICE_EMOTION,
    _TONE_TO_SPEED,
    _derive_voice_controls,
    _fetch_user_aura_profile,
)

_VALID_EMOTIONS = set(typing.get_args(TTSVoiceEmotion))


# --------------------------------------------------------------------------
# _derive_voice_controls — pure mapping
# --------------------------------------------------------------------------

def test_profileless_user_gets_default_voice():
    """The safety contract: no signals -> (None, None) -> byte-identical default voice."""
    assert _derive_voice_controls("", "") == (None, None)


def test_unknown_signals_fall_back_to_default():
    assert _derive_voice_controls("sarcastic", "elated") == (None, None)


@pytest.mark.parametrize("tone,expected_speed", list(_TONE_TO_SPEED.items()))
def test_each_tone_maps_to_its_speed(tone, expected_speed):
    speed, _ = _derive_voice_controls(tone, "")
    assert speed == expected_speed


@pytest.mark.parametrize("state,expected_emotion", list(_EMOTIONAL_STATE_TO_VOICE_EMOTION.items()))
def test_each_emotional_state_maps_to_its_emotion(state, expected_emotion):
    _, emotion = _derive_voice_controls("", state)
    assert emotion == expected_emotion


def test_tone_and_emotion_combine():
    speed, emotion = _derive_voice_controls("terse", "excited")
    assert speed == 1.08
    assert emotion == "Excited"


def test_casing_and_whitespace_are_normalized():
    assert _derive_voice_controls("  Playful ", "EXCITED") == (1.05, "Excited")


# --------------------------------------------------------------------------
# Invariants — these protect against a shipped typo in the mapping tables
# --------------------------------------------------------------------------

def test_every_speed_value_is_a_valid_sonic3_float():
    """sonic-3 raises ValueError on non-float speed; range warns outside 0.6-2.0."""
    for tone, speed in _TONE_TO_SPEED.items():
        assert isinstance(speed, float), f"{tone} speed must be float"
        assert 0.6 <= speed <= 2.0, f"{tone} speed {speed} out of sonic-3 range"


def test_every_emotion_value_is_a_real_cartesia_emotion():
    """A typo'd emotion would 400 every sonic-3 turn and silently fall back to Deepgram."""
    for state, emotion in _EMOTIONAL_STATE_TO_VOICE_EMOTION.items():
        assert emotion in _VALID_EMOTIONS, f"{state} -> {emotion!r} is not a TTSVoiceEmotion"


# --------------------------------------------------------------------------
# _fetch_user_aura_profile — single-read reshape + argmax
# --------------------------------------------------------------------------

def _patch_aura_doc(data: dict | None):
    """Patch admin_firestore so UserAura/{uid}.get().to_dict() returns `data`."""
    snapshot = MagicMock()
    snapshot.to_dict.return_value = data
    db = MagicMock()
    db.collection.return_value.document.return_value.get.return_value = snapshot
    return patch("src.agent.voice_agent.admin_firestore", return_value=db)


async def test_empty_doc_returns_empty_signals():
    with _patch_aura_doc(None):
        result = await _fetch_user_aura_profile("u1")
    assert result == {"summary": "", "dominant_tone": "", "dominant_emotion": ""}


async def test_dominant_emotion_is_argmax_of_emotional_signals():
    data = {
        "dominant_tone": "playful",
        "emotional_signals": {"anxious": 2, "excited": 9, "neutral": 4},
    }
    with _patch_aura_doc(data):
        result = await _fetch_user_aura_profile("u1")
    assert result["dominant_tone"] == "playful"
    assert result["dominant_emotion"] == "excited"


async def test_missing_emotional_signals_yields_empty_emotion():
    data = {"dominant_tone": "terse"}
    with _patch_aura_doc(data):
        result = await _fetch_user_aura_profile("u1")
    assert result["dominant_tone"] == "terse"
    assert result["dominant_emotion"] == ""


async def test_summary_block_is_still_populated():
    """The reshape must not break the prompt block the rest of the session relies on."""
    data = {
        "dominant_tone": "casual",
        "response_depth_preference": "wants_brief",
        "explicit_facts": ["likes hiking"],
    }
    with _patch_aura_doc(data):
        result = await _fetch_user_aura_profile("u1")
    assert "Communication style: casual, wants_brief" in result["summary"]
    assert "likes hiking" in result["summary"]
