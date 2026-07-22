"""Coverage for desktop screen frames injected into a live voice session.

Pins the contracts that keep screen sight correct and every other session unharmed:
  - frames assemble from byte-stream chunks with their client-stamped attributes;
  - the size cap and empty streams drop loudly instead of buffering garbage;
  - a stale frame is never injected (it no longer reflects "their screen right now");
  - a session that never received a frame passes through the turn hook UNTOUCHED —
    this is what preserves LiveKit preemptive generation for mobile/keyboard/unarmed
    desktop sessions;
  - injection appends the label + ImageContent to the user message, and the label
    changes text_content — the mechanism that invalidates the speculative imageless
    reply so the screenshot actually reaches the LLM;
  - earlier turns' images collapse to text placeholders on the SHARED message objects
    (turn_ctx is a shallow copy), so exactly one image is ever hot in context;
  - the hook helper never raises (a raised hook drops the whole turn reply).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from livekit.agents import llm as lk_llm

from src.agent.voice import screen_frames
from src.agent.voice.screen_frames import (
    ScreenFrame,
    ScreenFrameStore,
    attach_screen_frame_to_turn,
)
from src.agent.voice_prompt import VOICE_PROMPT, render_screen_sight_note


class _FakeReader:
    """Mimics rtc.ByteStreamReader: async chunk iterator plus .info.attributes."""

    def __init__(self, chunks: list[bytes], attributes: dict | None = None):
        self._chunks = list(chunks)
        self.info = SimpleNamespace(attributes=attributes or {})

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _store() -> ScreenFrameStore:
    return ScreenFrameStore(session_id="sid12345", user_id="uid")


def _fresh_frame(jpeg: bytes = b"\xff\xd8jpegdata", **attrs) -> ScreenFrame:
    attributes = {"frame_id": "f1", "jpeg_width_px": "1280", "jpeg_height_px": "720"}
    attributes.update(attrs)
    return ScreenFrame(
        jpeg_bytes=jpeg, attributes=attributes, received_at_monotonic=time.monotonic()
    )


# ---------------------------------------------------------------- assembly


async def test_assembles_chunks_with_attributes():
    store = _store()
    reader = _FakeReader(
        [b"abc", b"def"],
        {"frame_id": "f9", "jpeg_width_px": "1280", "jpeg_height_px": "720"},
    )
    await store._assemble_frame(reader, "uid")
    frame = await store.fresh_frame()
    assert frame is not None
    assert frame.jpeg_bytes == b"abcdef"
    assert frame.frame_id == "f9"
    assert frame.width_px == 1280 and frame.height_px == 720


async def test_oversize_frame_dropped():
    store = _store()
    big = b"x" * (screen_frames._MAX_FRAME_BYTES + 1)
    await store._assemble_frame(_FakeReader([big]), "uid")
    assert not store.has_ever_received_frame
    assert await store.fresh_frame() is None


async def test_empty_stream_dropped():
    store = _store()
    await store._assemble_frame(_FakeReader([]), "uid")
    assert not store.has_ever_received_frame


async def test_newest_frame_wins():
    store = _store()
    await store._assemble_frame(_FakeReader([b"old"], {"frame_id": "f1"}), "uid")
    await store._assemble_frame(_FakeReader([b"new"], {"frame_id": "f2"}), "uid")
    frame = await store.fresh_frame()
    assert frame is not None and frame.frame_id == "f2"


async def test_malformed_attribute_ints_are_none():
    frame = _fresh_frame(jpeg_width_px="not-a-number")
    assert frame.width_px is None
    assert frame.height_px == 720


# ---------------------------------------------------------------- freshness


async def test_stale_frame_not_returned():
    store = _store()
    store._latest = ScreenFrame(
        jpeg_bytes=b"old",
        attributes={},
        received_at_monotonic=time.monotonic() - screen_frames._FRAME_MAX_AGE_S - 1,
    )
    assert await store.fresh_frame() is None
    # But the session still counts as having received one (hook must keep stripping).
    assert store.has_ever_received_frame


class _SlowReader(_FakeReader):
    """Each chunk takes a beat to arrive, keeping the transfer in flight."""

    async def __anext__(self) -> bytes:
        await asyncio.sleep(0.05)
        return await super().__anext__()


async def test_fresh_frame_waits_for_inflight_transfer():
    store = _store()
    reader = _SlowReader([b"late-frame"], {"frame_id": "late"})
    task = asyncio.create_task(store._assemble_frame(reader, "uid"))
    await asyncio.sleep(0)  # let the assembly task start so the transfer is in flight
    frame = await store.fresh_frame()
    assert frame is not None and frame.frame_id == "late"
    await task


# ---------------------------------------------------------------- turn hook


def _turn(*, prior_image: bool = False) -> tuple[lk_llm.ChatContext, lk_llm.ChatMessage]:
    ctx = lk_llm.ChatContext()
    if prior_image:
        ctx.items.append(
            lk_llm.ChatMessage(
                role="user",
                content=[
                    "earlier question",
                    lk_llm.ImageContent(image="data:image/jpeg;base64,QUFB"),
                ],
            )
        )
    message = lk_llm.ChatMessage(role="user", content=["what am i looking at"])
    return ctx, message


async def test_no_frames_ever_is_a_strict_noop():
    store = _store()
    ctx, message = _turn(prior_image=True)
    prior_message = ctx.items[0]
    assert isinstance(prior_message, lk_llm.ChatMessage)
    items_before = list(ctx.items)
    content_before = list(message.content)
    prior_content_before = list(prior_message.content)

    await attach_screen_frame_to_turn(store, ctx, message, session_id="s", user_id="u")

    # Nothing touched: preemptive generation must survive frame-less sessions.
    assert ctx.items == items_before
    assert message.content == content_before
    assert prior_message.content == prior_content_before


async def test_fresh_frame_injects_label_and_image():
    store = _store()
    store._latest = _fresh_frame()
    ctx, message = _turn()
    text_before = message.text_content

    await attach_screen_frame_to_turn(store, ctx, message, session_id="s", user_id="u")

    image_part = message.content[-1]
    assert isinstance(image_part, lk_llm.ImageContent)
    assert isinstance(image_part.image, str)
    assert image_part.image.startswith("data:image/jpeg;base64,")
    assert image_part.mime_type == "image/jpeg"
    label = message.content[-2]
    assert isinstance(label, str) and "1280x720" in label
    # The label must change text_content: that is what invalidates the speculative
    # imageless reply so the screenshot actually reaches the LLM.
    assert message.text_content != text_before


async def test_prior_images_collapse_to_placeholder_on_shared_objects():
    store = _store()
    store._latest = _fresh_frame()
    ctx, message = _turn(prior_image=True)
    prior_message = ctx.items[0]
    assert isinstance(prior_message, lk_llm.ChatMessage)

    # Shallow copy, as AgentActivity builds it: same message objects, new list.
    turn_ctx_copy = lk_llm.ChatContext()
    turn_ctx_copy.items.extend(ctx.items)

    await attach_screen_frame_to_turn(
        store, turn_ctx_copy, message, session_id="s", user_id="u"
    )

    # The swap must land on the SHARED object so the agent's real history is cleaned.
    assert prior_message.content[1] == screen_frames._STALE_IMAGE_PLACEHOLDER
    assert not any(isinstance(p, lk_llm.ImageContent) for p in prior_message.content)


async def test_stale_frame_strips_but_does_not_inject():
    store = _store()
    store._latest = ScreenFrame(
        jpeg_bytes=b"old",
        attributes={},
        received_at_monotonic=time.monotonic() - screen_frames._FRAME_MAX_AGE_S - 1,
    )
    ctx, message = _turn(prior_image=True)
    prior_message = ctx.items[0]
    assert isinstance(prior_message, lk_llm.ChatMessage)

    await attach_screen_frame_to_turn(store, ctx, message, session_id="s", user_id="u")

    assert not any(isinstance(p, lk_llm.ImageContent) for p in prior_message.content)
    assert not any(isinstance(p, lk_llm.ImageContent) for p in message.content)


async def test_hook_never_raises():
    class _ExplodingStore(ScreenFrameStore):
        def __init__(self):
            super().__init__(session_id="s", user_id="u")
            self._latest = _fresh_frame()

        async def fresh_frame(self):
            raise RuntimeError("boom")

    ctx, message = _turn()
    # Must swallow: a raised hook makes LiveKit drop the entire turn reply.
    await attach_screen_frame_to_turn(
        _ExplodingStore(), ctx, message, session_id="s", user_id="u"
    )


# ---------------------------------------------------------------- prompt slot


def test_screen_sight_note_renders_only_for_desktop():
    note = render_screen_sight_note("desktop")
    assert "control alt S" in note
    assert "screenshot" in note
    # The prompt-injection posture rides with the capability.
    assert "never" in note.lower() and "instructions" in note.lower()
    assert render_screen_sight_note("app") == ""
    assert render_screen_sight_note("keyboard") == ""


def test_voice_prompt_formats_with_screen_sight_slot():
    vars = {
        "name": "V",
        "timezone": "UTC",
        "local_time": "9:00 AM",
        "local_date": "July 2",
        "memory_summary": "",
        "graph_context": "",
        "last_session_context": "",
        "last_session_at": "",
        "archive_context": "",
        "user_aura_profile": "",
        "surface": "",
        "screen_sight": render_screen_sight_note("desktop"),
    }
    rendered = VOICE_PROMPT.format(**vars)
    assert "# Seeing their screen" in rendered
    vars["screen_sight"] = render_screen_sight_note("app")
    rendered_mobile = VOICE_PROMPT.format(**vars)
    assert "# Seeing their screen" not in rendered_mobile
