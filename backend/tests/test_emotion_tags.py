"""Tests for bracket audio cues -> Cartesia sonic-3 inline speech markup.

Four layers under test, mirroring how the reply text actually flows:
  1. convert_audio_cues_for_sonic3 — pure cue -> markup conversion + the
     hallucinated-cue killer ([soft laughter] must die here, not reach TTS).
  2. convert_audio_cue_stream — the same conversion across chunk boundaries,
     with the shared bracket-cue holdback.
  3. BuddyAgent.tts_node wiring — the default TTS node must receive markdown-free
     text with cues already converted; the caption path must never see a cue.
  4. SpeechMarkupStrippingTTS + build_tts_pipeline — the fallback engines must
     never be handed sonic-3-only markup they would read aloud.
"""

from __future__ import annotations

import pytest
from livekit.agents import tts as lk_tts

from src.agent.voice.emotion_tags import (
    CARTESIA_SONIC3_EMOTION_NAMES,
    DELIVERY_CUE_TO_INLINE_SSML,
    convert_audio_cue_stream,
    convert_audio_cues_for_sonic3,
    strip_inline_speech_markup,
)
from src.agent.voice.fallback_tts_wrapper import SpeechMarkupStrippingTTS
from src.agent.voice.text_sanitizer import strip_nonverbal_cue_stream


# --------------------------------------------------------------------------
# convert_audio_cues_for_sonic3 — pure conversion
# --------------------------------------------------------------------------

def test_emotion_cue_converts_to_inline_markup():
    out = convert_audio_cues_for_sonic3("[excited] no shot, you got the offer?")
    assert out == '<emotion value="excited"/> no shot, you got the offer?'


def test_cue_lookup_is_case_insensitive():
    assert convert_audio_cues_for_sonic3("[Excited] yo") == '<emotion value="excited"/> yo'


def test_whisper_delivery_cue_expands_to_volume_and_speed():
    out = convert_audio_cues_for_sonic3("[whisper] okay, lowkey, between us.")
    assert out == '<volume ratio="0.6"/><speed ratio="0.9"/> okay, lowkey, between us.'


def test_hyped_delivery_cue_expands_to_compound_markup():
    out = convert_audio_cues_for_sonic3("[hyped] NO WAY.")
    assert out == '<volume ratio="1.3"/><speed ratio="1.1"/><emotion value="excited"/> NO WAY.'


def test_laughter_cue_is_kept_verbatim_for_cartesia():
    text = "Bro, for real? [laughter] no way"
    assert convert_audio_cues_for_sonic3(text) == text


def test_hallucinated_cues_are_stripped_before_tts():
    # The dead-air bug: an unsupported cue used to reach Cartesia as silence.
    out = convert_audio_cues_for_sonic3("[soft laughter] got you, chilling here.")
    assert "soft" not in out
    assert "[" not in out
    assert "got you, chilling here." in out
    assert convert_audio_cues_for_sonic3("[chuckle]") == ""
    assert "yeah" in convert_audio_cues_for_sonic3("[sigh] yeah")


def test_point_tags_and_footnotes_are_never_touched():
    assert (
        convert_audio_cues_for_sonic3("here [POINT:640,360:save]")
        == "here [POINT:640,360:save]"
    )
    assert convert_audio_cues_for_sonic3("footnote [1] stays") == "footnote [1] stays"


def test_empty_string_passes_through():
    assert convert_audio_cues_for_sonic3("") == ""


def test_conversion_fails_open_on_internal_error(monkeypatch):
    from src.agent.voice import emotion_tags

    class ExplodingPattern:
        def sub(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(emotion_tags, "_BRACKET_CUE", ExplodingPattern())
    assert convert_audio_cues_for_sonic3("[excited] hey") == "[excited] hey"


def test_delivery_cue_names_never_collide_with_emotion_names():
    """A delivery cue shadowing a real emotion name would silently change meaning."""
    assert not set(DELIVERY_CUE_TO_INLINE_SSML) & CARTESIA_SONIC3_EMOTION_NAMES


# --------------------------------------------------------------------------
# strip_inline_speech_markup — what the fallback engines receive
# --------------------------------------------------------------------------

def test_strip_removes_all_three_markup_kinds_and_cues():
    text = (
        '<volume ratio="0.6"/><speed ratio="0.9"/><emotion value="excited"/> hey '
        "[laughter] there [excited] friend"
    )
    assert strip_inline_speech_markup(text) == "hey there friend"


def test_strip_leaves_plain_words_intact():
    assert strip_inline_speech_markup("yeah, tuesday works.") == "yeah, tuesday works."


def test_strip_fails_open_on_internal_error(monkeypatch):
    from src.agent.voice import emotion_tags

    class ExplodingPattern:
        def sub(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(emotion_tags, "_INLINE_SPEECH_MARKUP", ExplodingPattern())
    text = '<emotion value="excited"/> hey'
    assert strip_inline_speech_markup(text) == text


# --------------------------------------------------------------------------
# convert_audio_cue_stream — cross-chunk holdback
# --------------------------------------------------------------------------

async def _collect(chunks: list[str]) -> list[str]:
    async def gen():
        for piece in chunks:
            yield piece

    return [seg async for seg in convert_audio_cue_stream(gen())]


@pytest.mark.asyncio
async def test_stream_converts_cue_split_across_chunks():
    out = "".join(await _collect(["[exci", "ted] yo"]))
    assert out == '<emotion value="excited"/> yo'


@pytest.mark.asyncio
async def test_stream_strips_unknown_cue_split_across_chunks():
    out = "".join(await _collect(["ok ", "[soft ", "laughter] done"]))
    assert "laughter" not in out
    assert "done" in out
    assert "[" not in out


@pytest.mark.asyncio
async def test_stream_emits_unterminated_bracket_at_end_as_is():
    # A bare "[..." that never closes was never a cue: fail open, speak it.
    out = "".join(await _collect(["hey [wait"]))
    assert out == "hey [wait"


@pytest.mark.asyncio
async def test_stream_never_yields_a_partial_tag():
    segments = await _collect(["[whis", "per] hey. ", "[exci", "ted] yo [laughter]"])
    for segment in segments:
        assert segment.count("<") == segment.count(">")
        # No segment may end inside a still-open cue (the holdback's whole job).
        assert not segment.endswith("[exci")
        assert not segment.endswith("[whis")


# --------------------------------------------------------------------------
# Live wiring — tts_node converts, the caption path stays cue-free
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buddy_tts_node_converts_cues_after_markdown_strip(monkeypatch):
    """The default TTS node must receive markdown-free text with cues converted.

    Verifies the real chain (sanitize_text_stream -> convert_audio_cue_stream ->
    Agent.default.tts_node) by stubbing the default node to record its input.
    """
    from types import SimpleNamespace

    from livekit.agents import Agent

    from src.agent.buddy_agent import BuddyAgent

    received: list[str] = []

    async def fake_default_tts_node(agent, text, model_settings):
        async for chunk in text:
            received.append(chunk)
        return
        yield  # unreachable; makes this an async generator

    monkeypatch.setattr(Agent.default, "tts_node", fake_default_tts_node)

    async def reply_stream():
        for piece in [
            "**Big** news. [exci",
            "ted] you got it! ",
            "[soft laughter] wild. [laughter] for real.\n",
        ]:
            yield piece

    frames = [f async for f in BuddyAgent.tts_node(SimpleNamespace(), reply_stream(), None)]

    joined = "".join(received)
    assert "*" not in joined                              # markdown stripped first
    assert '<emotion value="excited"/>' in joined         # cue converted, unsplit
    assert "[laughter]" in joined                         # the one cue Cartesia speaks
    assert "soft laughter" not in joined                  # hallucinated cue killed
    assert "Big news." in joined
    assert frames == []


@pytest.mark.asyncio
async def test_caption_path_still_strips_emotion_and_delivery_cues():
    """Regression pin: captions must never show a cue, converted or not."""

    async def gen():
        for piece in ["[whis", "per] hey ", "[excited] yo"]:
            yield piece

    out = "".join([seg async for seg in strip_nonverbal_cue_stream(gen())])
    assert out == "hey yo"


# --------------------------------------------------------------------------
# SpeechMarkupStrippingTTS — the fallback engines never see sonic-3 markup
# --------------------------------------------------------------------------

class _RecordingFakeTTS(lk_tts.TTS):
    def __init__(self) -> None:
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=24000,
            num_channels=1,
        )
        self.synthesized_texts: list[str] = []
        self.prewarmed = False
        self.closed = False

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def provider(self) -> str:
        return "fake-provider"

    def synthesize(self, text, *, conn_options=None):
        self.synthesized_texts.append(text)
        return None  # a real ChunkedStream is irrelevant to what we assert

    def prewarm(self) -> None:
        self.prewarmed = True

    async def aclose(self) -> None:
        self.closed = True


def test_wrapper_strips_markup_before_delegating():
    inner = _RecordingFakeTTS()
    wrapper = SpeechMarkupStrippingTTS(inner)
    wrapper.synthesize('<emotion value="excited"/> hey [laughter] there')
    assert inner.synthesized_texts == ["hey there"]


def test_wrapper_declares_non_streaming_and_passes_identity_through():
    inner = _RecordingFakeTTS()
    wrapper = SpeechMarkupStrippingTTS(inner)
    # streaming=False is load-bearing: FallbackAdapter then feeds synthesize()
    # whole sentences via its own StreamAdapter, so tags are never split.
    assert wrapper.capabilities.streaming is False
    assert wrapper.model == "fake-model"
    assert wrapper.provider == "fake-provider"
    assert wrapper.sample_rate == inner.sample_rate
    assert wrapper.num_channels == inner.num_channels


def test_wrapper_reemits_inner_metrics_and_prewarms():
    inner = _RecordingFakeTTS()
    wrapper = SpeechMarkupStrippingTTS(inner)
    seen: list[object] = []
    wrapper.on("metrics_collected", lambda payload: seen.append(payload))
    inner.emit("metrics_collected", {"ttfb": 0.1})
    assert seen == [{"ttfb": 0.1}]
    wrapper.prewarm()
    assert inner.prewarmed is True


@pytest.mark.asyncio
async def test_wrapper_aclose_closes_inner():
    inner = _RecordingFakeTTS()
    wrapper = SpeechMarkupStrippingTTS(inner)
    await wrapper.aclose()
    assert inner.closed is True


# --------------------------------------------------------------------------
# build_tts_pipeline — wrapper placement
# --------------------------------------------------------------------------

def test_pipeline_wraps_only_the_fallback_engines(monkeypatch):
    from livekit.plugins import cartesia, deepgram

    from src.agent.voice.pipelines import build_tts_pipeline
    from src.config.settings import settings

    monkeypatch.setattr(settings, "CARTESIA_API_KEY", "fake-cartesia-key")
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "fake-deepgram-key")

    adapter = build_tts_pipeline({})
    primary, first_fallback, second_fallback = adapter._tts_instances
    # sonic-3 understands the markup and MUST receive it unstripped.
    assert isinstance(primary, cartesia.TTS)
    assert not isinstance(primary, SpeechMarkupStrippingTTS)
    assert isinstance(first_fallback, SpeechMarkupStrippingTTS)
    assert isinstance(first_fallback._wrapped_tts, deepgram.TTS)
    assert isinstance(second_fallback, SpeechMarkupStrippingTTS)
    assert isinstance(second_fallback._wrapped_tts, cartesia.TTS)
