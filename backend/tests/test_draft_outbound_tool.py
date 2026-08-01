"""
Coverage for the voice worker's Buddy Drafts tool (agent/voice/draft_outbound).

Pins the branch logic that guards cost and privacy:
  - a new draft uses the finalized transcript verbatim and reads the fresh frame;
  - no screen frame publishes draft.failed{no_frame} and speaks the retry line;
  - the free-tier daily cap is charged ONCE per new draft, prod-only, and a
    voice refine never touches the counter or the frame store;
  - a refine with no current draft fails truthfully and creates nothing;
  - happy paths publish draft.generating -> draft.created / draft.updated with
    the right revisions and keep session state consistent;
  - a drafter failure publishes draft.failed and degrades to speech.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src.agent.voice import draft_outbound as dm
from src.services.outbound_draft.drafter import OutboundDraftResult

_DRAFT_ID = "d" * 32


class _FakeFrame:
    jpeg_bytes = b"\xff\xd8fakejpeg"
    width_px = 1280
    height_px = 720


class _FakeFrameStore:
    def __init__(self, frame=None):
        self._frame = frame
        self.calls = 0

    async def fresh_frame(self):
        self.calls += 1
        return self._frame


class _Harness:
    """Wires every external seam of run_draft_tool to recorders."""

    def __init__(self, monkeypatch, *, production=False, quota_allowed=True):
        self.published: list[dict] = []
        self.captured: list[dict] = []
        self.quota_calls: list[str] = []
        self.draft_calls: list[dict] = []
        self.refine_calls: list[dict] = []
        self.draft_result = OutboundDraftResult(
            text="hey Sarah, sounds great", context_summary="reply to Sarah", reason="ok"
        )
        self.refine_result = OutboundDraftResult(
            text="hey Sarah, warmer now", context_summary="reply to Sarah", reason="ok"
        )

        async def _publish_data(payload, reliable=True):
            self.published.append(json.loads(payload.decode("utf-8")))

        fake_room = SimpleNamespace(
            local_participant=SimpleNamespace(publish_data=_publish_data)
        )
        monkeypatch.setattr(
            dm, "get_job_context", lambda: SimpleNamespace(room=fake_room)
        )
        monkeypatch.setattr(dm, "settings", SimpleNamespace(is_production=production))

        async def _capture(*, distinct_id, event, properties=None):
            self.captured.append({"event": event, "properties": properties or {}})

        monkeypatch.setattr(dm, "capture_event", _capture)

        async def _quota(uid):
            self.quota_calls.append(uid)
            return (quota_allowed, 1 if quota_allowed else 5)

        monkeypatch.setattr(
            dm, "check_and_increment_daily_outbound_draft_usage", _quota
        )

        async def _fetch(uid):
            return {}, []

        monkeypatch.setattr(dm, "fetch_cached_aura_data", _fetch)

        async def _draft(uid, **kwargs):
            self.draft_calls.append(kwargs)
            return self.draft_result

        async def _refine(uid, **kwargs):
            self.refine_calls.append(kwargs)
            return self.refine_result

        monkeypatch.setattr(dm, "draft_outbound", _draft)
        monkeypatch.setattr(dm, "refine_outbound", _refine)

    def event_types(self) -> list[str]:
        return [e["type"] for e in self.published]


def _state(tier="free"):
    return dm.DraftOutboundSession(
        user_id="uid1", session_id="sess1", user_tier=tier, display_name="Varun"
    )


async def test_new_uses_exact_finalized_transcript_and_fresh_frame(monkeypatch):
    h = _Harness(monkeypatch, production=True)
    state = _state()
    store = _FakeFrameStore(_FakeFrame())
    transcript = "  Draft a warm reply to Sarah in two sentences.  "

    spoken = await dm.run_draft_tool(
        state,
        store,
        operation="new",
        transcript=transcript,
    )

    assert spoken == dm.SPOKEN_DRAFT_READY
    assert store.calls == 1
    assert h.draft_calls[0]["intent"] == transcript
    assert h.draft_calls[0]["channel"] == dm.DEFAULT_CHANNEL
    assert h.draft_calls[0]["length"] == "medium"
    assert h.draft_calls[0]["recipient_hint"] == ""
    assert h.draft_calls[0]["jpeg_base64"]
    assert h.event_types() == ["draft.generating", "draft.created"]
    assert state.current is not None


async def test_no_frame_fails_loudly_without_quota(monkeypatch):
    h = _Harness(monkeypatch, production=True)
    spoken = await dm.run_draft_tool(
        _state(),
        _FakeFrameStore(None),
        operation="new",
        transcript="decline",
    )
    assert spoken == dm.SPOKEN_NO_FRAME
    assert h.event_types() == ["draft.failed"]
    assert h.published[0]["payload"]["reason"] == "no_frame"
    assert h.quota_calls == []  # a no-frame miss never burns quota


async def test_quota_exceeded_speaks_limit_and_captures(monkeypatch):
    h = _Harness(monkeypatch, production=True, quota_allowed=False)
    spoken = await dm.run_draft_tool(
        _state(tier="free"),
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
    )
    assert spoken == dm.SPOKEN_QUOTA
    assert h.event_types() == ["draft.failed"]
    assert h.published[0]["payload"]["reason"] == "quota_exceeded"
    assert [c["event"] for c in h.captured] == ["desktop_draft_limit_hit"]
    assert h.draft_calls == []


async def test_paid_tier_skips_quota(monkeypatch):
    h = _Harness(monkeypatch, production=True)
    await dm.run_draft_tool(
        _state(tier="pro"),
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
    )
    assert h.quota_calls == []
    assert h.event_types() == ["draft.generating", "draft.created"]


async def test_non_production_skips_quota(monkeypatch):
    h = _Harness(monkeypatch, production=False)
    await dm.run_draft_tool(
        _state(tier="free"),
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
    )
    assert h.quota_calls == []


async def test_new_draft_happy_path(monkeypatch):
    h = _Harness(monkeypatch, production=True)
    state = _state(tier="free")

    spoken = await dm.run_draft_tool(
        state,
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="politely decline",
    )

    assert spoken == dm.SPOKEN_DRAFT_READY
    assert h.quota_calls == ["uid1"]  # charged exactly once
    assert h.event_types() == ["draft.generating", "draft.created"]
    created = h.published[1]["payload"]
    assert created["revision"] == 1
    assert created["text"] == "hey Sarah, sounds great"
    assert created["context_summary"] == "reply to Sarah"
    assert state.current is not None
    assert state.current.draft_id == created["draft_id"]
    # The frame reached the drafter as base64 with its dimensions.
    call = h.draft_calls[0]
    assert call["jpeg_base64"] and call["jpeg_width"] == 1280
    assert call["display_name"] == "Varun"
    assert [c["event"] for c in h.captured] == ["desktop_draft_requested"]
    props = h.captured[0]["properties"]
    assert props == {"channel": "on_screen", "length": "medium", "mode": "new"}


async def test_voice_refine_skips_quota_and_frame(monkeypatch):
    h = _Harness(monkeypatch, production=True)
    state = _state(tier="free")
    state.current = dm.DraftState(
        draft_id=_DRAFT_ID, channel="email_reply", length="short",
        text="hi Sarah, can't make it", context_summary="declining Sarah",
        recipient_hint="Sarah", revision=1,
    )
    store = _FakeFrameStore(None)  # even with no frame, refine must work

    spoken = await dm.run_draft_tool(
        state,
        store,
        operation="refine",
        transcript="  warmer, please  ",
    )

    assert spoken == dm.SPOKEN_REFINE_READY
    assert store.calls == 0 and h.quota_calls == []
    assert h.event_types() == ["draft.generating", "draft.updated"]
    assert h.published[0]["payload"]["mode"] == "refine"
    updated = h.published[1]["payload"]
    assert updated["draft_id"] == _DRAFT_ID
    assert updated["revision"] == 2
    assert updated["length"] == "short"
    assert updated["text"] == "hey Sarah, warmer now"
    assert updated["artifact"]["body"] == updated["text"]
    assert h.published[1]["schema_version"] == 2
    assert state.current.revision == 2
    assert state.current.text == "hey Sarah, warmer now"
    # The stored screen-derived summary rode into the refine call.
    assert h.refine_calls[0]["context_summary"] == "declining Sarah"
    assert h.refine_calls[0]["refine_instruction"] == "  warmer, please  "


async def test_refine_without_current_draft_fails_without_creating(monkeypatch):
    h = _Harness(monkeypatch, production=False)
    state = _state()
    store = _FakeFrameStore(_FakeFrame())

    spoken = await dm.run_draft_tool(
        state,
        store,
        operation="refine",
        transcript="ask about the open role",
    )

    assert spoken == dm.SPOKEN_NO_CURRENT_DRAFT
    assert store.calls == 0
    assert h.quota_calls == []
    assert h.refine_calls == []
    assert h.draft_calls == []
    assert h.published == []
    assert state.current is None


async def test_drafter_failure_publishes_failed(monkeypatch):
    h = _Harness(monkeypatch, production=False)
    h.draft_result = OutboundDraftResult(reason="timeout")

    spoken = await dm.run_draft_tool(
        _state(),
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
    )

    assert spoken == dm.SPOKEN_FAILED
    assert h.event_types() == ["draft.generating", "draft.failed"]
    assert h.published[1]["payload"]["reason"] == "timeout"
    assert h.captured == []  # no success event for a failed draft


# ------------------------------------------------ async-tool path (Phase 2)


class _FakeRunCtx:
    """Minimal RunContext for direct acknowledgement plus filler speech."""

    def __init__(
        self,
        *,
        say_raises=False,
        say_hangs=False,
        filler_enter_raises=False,
        filler_exit_raises=False,
    ) -> None:
        self.said: list[str] = []
        self.filler_entered = False
        self._say_raises = say_raises
        self._say_hangs = say_hangs
        self._filler_enter_raises = filler_enter_raises
        self._filler_exit_raises = filler_exit_raises
        self.session = SimpleNamespace(say=self._say)

    async def _say(self, message) -> None:
        if self._say_hangs:
            await asyncio.sleep(3600)
        if self._say_raises:
            raise RuntimeError("say boom")
        self.said.append(message)

    def with_filler(self, source, *, delay=0, interval=None, max_steps=None):
        outer = self

        class _CM:
            async def __aenter__(self):
                if outer._filler_enter_raises:
                    raise RuntimeError("filler enter boom")
                outer.filler_entered = True
                return None

            async def __aexit__(self, *exc):
                if outer._filler_exit_raises:
                    raise RuntimeError("filler exit boom")
                return False

        return _CM()


async def test_draft_path_speaks_direct_ack_and_creates_draft(monkeypatch):
    h = _Harness(monkeypatch, production=True)
    state = _state(tier="pro")
    ctx = _FakeRunCtx()

    spoken = await dm.run_draft_tool(
        state,
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
        run_ctx=ctx,
    )

    assert spoken == dm.SPOKEN_DRAFT_READY
    assert ctx.said == [dm.SPOKEN_DRAFT_STARTED]
    assert ctx.filler_entered is True
    assert h.event_types() == ["draft.generating", "draft.created"]
    assert len(h.draft_calls) == 1  # generated exactly once
    assert state.current is not None


async def test_filler_exit_failure_keeps_the_good_draft(monkeypatch):
    """The regression this hardening exists for: a filler-cleanup error after a
    successful draft must NOT unwind into SPOKEN_FAILED and discard it."""
    h = _Harness(monkeypatch, production=False)
    state = _state()
    ctx = _FakeRunCtx(filler_exit_raises=True)

    spoken = await dm.run_draft_tool(
        state,
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
        run_ctx=ctx,
    )

    assert spoken == dm.SPOKEN_DRAFT_READY
    assert h.event_types() == ["draft.generating", "draft.created"]
    assert len(h.draft_calls) == 1  # kept, not regenerated
    assert state.current is not None


async def test_filler_enter_failure_regenerates_draft_once(monkeypatch):
    h = _Harness(monkeypatch, production=False)
    state = _state()
    ctx = _FakeRunCtx(filler_enter_raises=True)

    spoken = await dm.run_draft_tool(
        state,
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
        run_ctx=ctx,
    )

    assert spoken == dm.SPOKEN_DRAFT_READY
    assert len(h.draft_calls) == 1  # generated once via the result-is-None fallback
    assert state.current is not None


async def test_direct_ack_failure_does_not_block_draft(monkeypatch):
    h = _Harness(monkeypatch, production=False)
    state = _state()
    ctx = _FakeRunCtx(say_raises=True)

    spoken = await dm.run_draft_tool(
        state,
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
        run_ctx=ctx,
    )

    assert spoken == dm.SPOKEN_DRAFT_READY
    assert ctx.said == []
    assert h.event_types() == ["draft.generating", "draft.created"]
    assert state.current is not None


async def test_direct_ack_hang_times_out_and_draft_proceeds(monkeypatch):
    monkeypatch.setattr(dm, "_DIRECT_SPEECH_TIMEOUT_S", 0.05)
    _Harness(monkeypatch, production=False)  # wires the seams via monkeypatch
    state = _state()
    ctx = _FakeRunCtx(say_hangs=True)

    spoken = await dm.run_draft_tool(
        state,
        _FakeFrameStore(_FakeFrame()),
        operation="new",
        transcript="decline",
        run_ctx=ctx,
    )

    assert spoken == dm.SPOKEN_DRAFT_READY
    assert ctx.said == []
    assert state.current is not None
