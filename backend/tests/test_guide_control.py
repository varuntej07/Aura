"""Agent-requested Guide Mode arm/disarm publish behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.agent.voice import guide_control as gc


async def test_enable_publishes_one_request_and_speaks_starting(monkeypatch):
    published: list[tuple[bytes, bool]] = []

    async def _publish(data, reliable):
        published.append((data, reliable))

    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=_publish))
    monkeypatch.setattr(gc, "current_room", lambda: room)

    spoken = await gc.request_guide_mode(user_id="u", session_id="s", enable=True)

    assert spoken == gc.SPOKEN_GUIDE_STARTING
    assert len(published) == 1 and published[0][1] is True
    event = json.loads(published[0][0])
    assert event == {"type": "guide.request", "payload": {"enable": True}}


async def test_disable_publishes_enable_false_and_speaks_stopping(monkeypatch):
    published: list[bytes] = []

    async def _publish(data, reliable):  # noqa: ARG001 - reliable asserted elsewhere
        published.append(data)

    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=_publish))
    monkeypatch.setattr(gc, "current_room", lambda: room)

    spoken = await gc.request_guide_mode(user_id="u", session_id="s", enable=False)

    assert spoken == gc.SPOKEN_GUIDE_STOPPING
    event = json.loads(published[0])
    assert event["payload"]["enable"] is False


async def test_publish_failure_never_claims_the_switch_worked(monkeypatch):
    async def _publish(_data, reliable=True):  # noqa: ARG001
        raise RuntimeError("room disconnected")

    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=_publish))
    monkeypatch.setattr(gc, "current_room", lambda: room)

    spoken = await gc.request_guide_mode(user_id="u", session_id="s", enable=True)

    assert spoken == gc.SPOKEN_GUIDE_REQUEST_FAILED
    assert spoken != gc.SPOKEN_GUIDE_STARTING
