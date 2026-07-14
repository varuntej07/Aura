"""Coverage for [POINT:...] tag extraction from the voice LLM output stream.

Pins the contracts that keep the tag invisible and the pointer reliable:
  - every grammar variant parses (bare coords, label, screenN, none);
  - the tag is stripped from what TTS/captions/transcripts see, including when it
    arrives SPLIT across stream chunks (the holdback buffer's whole reason);
  - malformed tags fail open (pass through as text, never a crash);
  - an unterminated tag at stream end is dropped, never spoken;
  - non-text items (tool-call chunks) pass through untouched and in order;
  - text containing '[' that never becomes a tag is not swallowed.
"""

from __future__ import annotations

from livekit.agents import llm as lk_llm

from src.agent.voice.point_tag import (
    PointTarget,
    extract_point_tags,
    filter_point_tags,
    holdback_start,
)

# ---------------------------------------------------------------- extraction


def test_extracts_full_tag_with_label_and_screen():
    cleaned, targets = extract_point_tags(
        "it's right up there. [POINT:1100,42:color inspector:screen2]"
    )
    assert cleaned == "it's right up there. "
    assert targets == [
        PointTarget(x=1100, y=42, label="color inspector", screen=2)
    ]


def test_extracts_tag_without_label_or_screen():
    cleaned, targets = extract_point_tags("click that. [POINT:640,360]")
    assert cleaned == "click that. "
    assert targets == [PointTarget(x=640, y=360, label="", screen=None)]


def test_none_tag_strips_to_nothing_with_no_target():
    cleaned, targets = extract_point_tags("just a thought. [POINT:none]")
    assert cleaned == "just a thought. "
    assert targets == []


def test_malformed_tag_passes_through_untouched():
    text = "weird [POINT:abc,def:thing] token"
    cleaned, targets = extract_point_tags(text)
    assert cleaned == text
    assert targets == []


def test_coordinates_with_spaces_parse():
    _, targets = extract_point_tags("[POINT:12 , 34:target]")
    assert targets == [PointTarget(x=12, y=34, label="target", screen=None)]


# ---------------------------------------------------------------- holdback


def test_holdback_holds_partial_tag_prefix():
    assert holdback_start("click the button [POI") == len("click the button ")
    assert holdback_start("click [POINT:640,3") == len("click ")


def test_holdback_releases_non_tag_brackets():
    # '[x' can never become '[POINT:', so nothing is held.
    text = "arrays use [x] indexing"
    assert holdback_start(text) == len(text)


def test_holdback_nothing_without_bracket():
    assert holdback_start("plain sentence") == len("plain sentence")


# ---------------------------------------------------------------- streaming


async def _run_filter(chunks: list[object]) -> tuple[list[object], list[PointTarget]]:
    points: list[PointTarget] = []

    async def _stream():
        for chunk in chunks:
            yield chunk

    emitted = [item async for item in filter_point_tags(_stream(), on_point=points.append)]
    return emitted, points


def _texts(emitted: list[object]) -> str:
    parts = []
    for item in emitted:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, lk_llm.ChatChunk) and item.delta and item.delta.content:
            parts.append(item.delta.content)
    return "".join(parts)


async def test_tag_split_across_chunks_is_stripped_and_fired():
    emitted, points = await _run_filter(
        ["see that menu up top? ", "[POI", "NT:285,", "11:source control]"]
    )
    assert _texts(emitted) == "see that menu up top? "
    assert points == [PointTarget(x=285, y=11, label="source control", screen=None)]


async def test_tag_in_single_chunk_is_stripped():
    emitted, points = await _run_filter(["click commit. [POINT:640,360:commit button]"])
    assert _texts(emitted) == "click commit. "
    assert points == [PointTarget(x=640, y=360, label="commit button", screen=None)]


async def test_none_tag_fires_nothing():
    emitted, points = await _run_filter(["all set. [POINT:none]"])
    assert _texts(emitted) == "all set. "
    assert points == []


async def test_unterminated_tag_at_stream_end_is_dropped_not_spoken():
    emitted, points = await _run_filter(["okay so ", "[POINT:63"])
    assert _texts(emitted) == "okay so "
    assert points == []


async def test_bracket_text_that_never_becomes_a_tag_survives():
    emitted, points = await _run_filter(["arrays use [", "x] indexing"])
    assert _texts(emitted) == "arrays use [x] indexing"
    assert points == []


async def test_chat_chunks_are_reemitted_with_clean_text():
    chunk = lk_llm.ChatChunk(
        id="c1",
        delta=lk_llm.ChoiceDelta(role="assistant", content="done. [POINT:5,6:it]"),
    )
    emitted, points = await _run_filter([chunk])
    assert len(emitted) == 1
    reemitted = emitted[0]
    assert isinstance(reemitted, lk_llm.ChatChunk)
    assert reemitted.id == "c1"
    assert reemitted.delta is not None
    assert reemitted.delta.content == "done. "
    assert points == [PointTarget(x=5, y=6, label="it", screen=None)]


async def test_non_text_items_pass_through_in_order():
    tool_chunk = lk_llm.ChatChunk(id="t1", delta=None)
    emitted, _ = await _run_filter(["hello ", tool_chunk, "world"])
    assert emitted[0] == "hello "
    assert emitted[1] is tool_chunk
    assert _texts(emitted) == "hello world"


async def test_callback_exception_never_breaks_the_stream():
    def _explode(_target: PointTarget) -> None:
        raise RuntimeError("boom")

    async def _stream():
        yield "fine. [POINT:1,2:x]"

    emitted = [item async for item in filter_point_tags(_stream(), on_point=_explode)]
    assert "".join(e for e in emitted if isinstance(e, str)) == "fine. "
