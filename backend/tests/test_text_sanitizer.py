"""Tests for the voice TTS markdown sanitizer.

The three cases the PRE-DEMO gate requires (must pass before any live call):
  1. the real session 0ee06b42 markdown-list/bold sample -> clean prose, no `*`
  2. snake_case identifiers preserved (underscores are not emphasis)
  3. the literal WORD "asterisk" preserved
Plus a few guards on links/headers and the streaming wrapper.
"""

from __future__ import annotations

import pytest

from src.agent.voice.text_sanitizer import (
    sanitize_for_speech,
    sanitize_text_stream,
    strip_nonverbal_cue_stream,
    strip_nonverbal_cues,
)

# The actual assistant turn 35 from voice_sessions session 0ee06b42 (2026-05-29).
SAMPLE_0EE06B42 = (
    "Yeah, a website is absolutely key. It's your central hub for everything.\n"
    "For branding, lean hard into that voice-first, AI friend aspect.\n"
    "Then, to get the word out beyond just the app stores:\n"
    "*   **Content:** Start a blog or social media, talk about ADHD challenges.\n"
    "*   **Communities:** Engage with online ADHD groups on Reddit, Facebook, forums."
)


def test_strips_real_markdown_list_and_bold_sample():
    out = sanitize_for_speech(SAMPLE_0EE06B42)
    # No markdown artifacts that TTS would read aloud.
    assert "*" not in out
    assert "**" not in out
    # The actual words survive, including the de-bolded labels.
    assert "Content: Start a blog" in out
    assert "Communities: Engage with online ADHD groups" in out
    assert "website is absolutely key" in out


def test_preserves_snake_case_identifiers():
    out = sanitize_for_speech("call get_user_context and then web_surf now")
    assert "get_user_context" in out
    assert "web_surf" in out


def test_preserves_literal_word_asterisk():
    text = "I'll say the word asterisk out loud, not the symbol."
    out = sanitize_for_speech(text)
    assert "asterisk" in out
    assert out == text  # nothing to strip; sentence is untouched


def test_strips_headers_and_links_keep_text():
    assert sanitize_for_speech("# Big news") == "Big news"
    assert sanitize_for_speech("see [the docs](https://x.com/y) here") == "see the docs here"
    assert sanitize_for_speech("_really_ good") == "really good"


def test_hyphenated_words_survive():
    out = sanitize_for_speech("a voice-first, low-latency build")
    assert "voice-first" in out
    assert "low-latency" in out


@pytest.mark.asyncio
async def test_stream_wrapper_cleans_chunked_markdown():
    async def gen():
        # split the sample mid-markdown to exercise cross-chunk delimiters
        for piece in ["Then:\n*   **Content:** Start ", "a blog.\n*   **More:** stuff here."]:
            yield piece

    out = "".join([seg async for seg in sanitize_text_stream(gen())])
    assert "*" not in out
    assert "Content: Start a blog" in out
    assert "More: stuff here" in out


def test_strip_nonverbal_cues_removes_laughter():
    # The exact bug: unsupported [soft laughter] leaked as dead text on screen.
    assert strip_nonverbal_cues("[soft laughter] Got you, chilling here.") == "Got you, chilling here."
    # The supported cue is hidden from the caption too (it belongs to TTS only).
    assert (
        strip_nonverbal_cues("Bro, for real? [laughter] you've been grinding.")
        == "Bro, for real? you've been grinding."
    )


def test_strip_nonverbal_cues_leaves_point_and_footnote():
    # [POINT:...] carries digits/colons, a numeric footnote is digits: neither is a cue.
    assert strip_nonverbal_cues("here [POINT:640,360:save]") == "here [POINT:640,360:save]"
    assert strip_nonverbal_cues("footnote [1] stays") == "footnote [1] stays"


def test_sanitize_for_speech_keeps_laughter_for_tts():
    # The audio path MUST keep [laughter] so Cartesia actually laughs.
    assert sanitize_for_speech("Bro, for real? [laughter] no way") == "Bro, for real? [laughter] no way"


@pytest.mark.asyncio
async def test_cue_stream_catches_tag_split_across_chunks():
    async def gen():
        for piece in ["Bro ", "[laug", "hter] no", " way"]:
            yield piece

    out = "".join([seg async for seg in strip_nonverbal_cue_stream(gen())])
    assert out == "Bro no way"  # cue gone, no doubled space at the boundary


@pytest.mark.asyncio
async def test_cue_stream_strips_unsupported_soft_laughter():
    async def gen():
        for piece in ["ok ", "[soft ", "laughter] done"]:
            yield piece

    out = "".join([seg async for seg in strip_nonverbal_cue_stream(gen())])
    assert out == "ok done"


@pytest.mark.asyncio
async def test_buddy_transcription_node_strips_laughter_before_delegating(monkeypatch):
    """The transcription_node override must hand the DEFAULT node cue-free caption text.

    This is the branch that feeds the client caption + forwarded transcript; the
    [laughter] cue must be gone here while tts_node keeps it for audio.
    """
    from types import SimpleNamespace

    from livekit.agents import Agent

    from src.agent.buddy_agent import BuddyAgent

    received: list[str] = []

    async def fake_default_transcription_node(agent, text, model_settings):
        async for chunk in text:
            received.append(chunk)
        return
        yield  # unreachable; makes this an async generator

    monkeypatch.setattr(Agent.default, "transcription_node", fake_default_transcription_node)

    async def reply_stream():
        for piece in ["Bro, for real? [laugh", "ter] you've been grinding.\n"]:
            yield piece

    frames = [f async for f in BuddyAgent.transcription_node(SimpleNamespace(), reply_stream(), None)]

    joined = "".join(received)
    assert "laughter" not in joined  # cue hidden from the caption
    assert "Bro, for real? you've been grinding." in joined
    assert frames == []


@pytest.mark.asyncio
async def test_buddy_tts_node_sanitizes_before_delegating(monkeypatch):
    """The tts_node override must hand the DEFAULT node already-sanitized text.

    Verifies the live wiring (BuddyAgent.tts_node -> Agent.default.tts_node), not just the
    pure helper: we stub the default node to record what text it receives.
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
        for piece in ["Here's the plan:\n*   **Step one:** call ", "get_user_context now.\n"]:
            yield piece

    # Call the override unbound; it only uses `self` to forward to the (stubbed) default node.
    frames = [f async for f in BuddyAgent.tts_node(SimpleNamespace(), reply_stream(), None)]

    joined = "".join(received)
    assert "*" not in joined  # markdown stripped before TTS
    assert "Step one: call get_user_context now" in joined  # words + identifier preserved
    assert frames == []  # stub emits no audio frames
