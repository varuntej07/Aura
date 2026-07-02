"""Coverage for POST /desktop/screen-demo (the first-run screen-sight demo).

Pins the contracts of an attacker-reachable image endpoint:
  - auth is required (no uid -> 401);
  - malformed body / missing fields / oversize / invalid base64 -> 400, never a call;
  - coordinates the model picked outside the central band are clamped server-side
    (the pointer must never land under the taskbar);
  - a model failure or an unparsed response -> 502 with friendly copy;
  - a good observation returns the exact keys the desktop client reads.
"""

from __future__ import annotations

import base64
import json

from src.handlers import screen_demo
from src.handlers.screen_demo import (
    ScreenDemoObservation,
    clamp_to_central_band,
    handle_screen_demo,
)

_VALID_JPEG_B64 = base64.b64encode(b"\xff\xd8fakejpegdata").decode("ascii")


class _Req:
    """Minimal stand-in for a FastAPI Request (auth is monkeypatched)."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeProvider:
    def __init__(self, observation=None, error: Exception | None = None):
        self._observation = observation
        self._error = error
        self.calls: list[dict] = []

    async def balanced(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self._error is not None:
            raise self._error
        return self._observation


def _auth(monkeypatch, uid: str | None):
    monkeypatch.setattr(screen_demo, "resolve_user_id_from_request", lambda _r: uid)


def _body(**overrides):
    body = {
        "jpeg_base64": _VALID_JPEG_B64,
        "jpeg_width_px": 1280,
        "jpeg_height_px": 720,
    }
    body.update(overrides)
    return body


def _payload(response) -> dict:
    return json.loads(response.body)


# --- clamp unit tests --------------------------------------------------------


def test_clamp_pulls_edge_coordinates_into_the_central_band():
    assert clamp_to_central_band(0, 1280) == 256      # 20% floor
    assert clamp_to_central_band(1280, 1280) == 1024  # 80% ceiling
    assert clamp_to_central_band(640, 1280) == 640    # center passes through


# --- handler -----------------------------------------------------------------


async def test_unauthenticated_is_401(monkeypatch):
    _auth(monkeypatch, None)
    response = await handle_screen_demo(_Req(_body()))
    assert response.status_code == 401


async def test_invalid_json_is_400(monkeypatch):
    _auth(monkeypatch, "uid1")
    response = await handle_screen_demo(_Req(ValueError("bad json")))
    assert response.status_code == 400


async def test_missing_fields_are_400(monkeypatch):
    _auth(monkeypatch, "uid1")
    for broken in (
        _body(jpeg_base64=""),
        _body(jpeg_width_px=None),
        _body(jpeg_height_px=0),
        _body(jpeg_width_px="1280"),
    ):
        response = await handle_screen_demo(_Req(broken))
        assert response.status_code == 400


async def test_oversize_image_is_400(monkeypatch):
    _auth(monkeypatch, "uid1")
    huge = "A" * (screen_demo._MAX_IMAGE_BASE64_SIZE + 4)
    response = await handle_screen_demo(_Req(_body(jpeg_base64=huge)))
    assert response.status_code == 400


async def test_invalid_base64_is_400(monkeypatch):
    _auth(monkeypatch, "uid1")
    response = await handle_screen_demo(_Req(_body(jpeg_base64="not base64!!!")))
    assert response.status_code == 400


async def test_good_observation_returns_clamped_payload(monkeypatch):
    _auth(monkeypatch, "uid1")
    provider = _FakeProvider(observation=ScreenDemoObservation(
        comment="ooh, a fastapi backend", x=5, y=700, label="main.py tab",
    ))
    monkeypatch.setattr(screen_demo, "get_model_provider", lambda: provider)

    response = await handle_screen_demo(_Req(_body()))

    assert response.status_code == 200
    payload = _payload(response)
    assert payload["comment"] == "ooh, a fastapi backend"
    assert payload["label"] == "main.py tab"
    # x=5 clamps to the 20% floor of 1280; y=700 clamps to 80% of 720.
    assert payload["x"] == 256
    assert payload["y"] == 576
    # The image actually rode into the model call.
    call = provider.calls[0]
    assert call["images"][0]["data"] == _VALID_JPEG_B64
    assert call["response_model"] is ScreenDemoObservation


async def test_model_failure_is_502(monkeypatch):
    _auth(monkeypatch, "uid1")
    provider = _FakeProvider(error=RuntimeError("model down"))
    monkeypatch.setattr(screen_demo, "get_model_provider", lambda: provider)

    response = await handle_screen_demo(_Req(_body()))
    assert response.status_code == 502


async def test_unparsed_text_response_is_502(monkeypatch):
    _auth(monkeypatch, "uid1")
    provider = _FakeProvider(observation="just some text, not a model")
    monkeypatch.setattr(screen_demo, "get_model_provider", lambda: provider)

    response = await handle_screen_demo(_Req(_body()))
    assert response.status_code == 502
