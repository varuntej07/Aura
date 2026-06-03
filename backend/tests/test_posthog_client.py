"""
Behavioral coverage for the fail-safe server-side PostHog client.

The contract this guards: capture must NEVER raise into or block a scoring tick.
A missing key, an import/init failure, or a bad payload degrades to a no-op plus
a log line — nothing more. These tests pin every one of those branches so a
future refactor can't quietly turn an analytics hiccup into a tick crash.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.services.analytics import posthog_client


@pytest.fixture(autouse=True)
def reset_memoised_client():
    """The client is memoised at module scope; reset it around every test so
    one test's configured/unconfigured state can't leak into the next."""
    posthog_client._client = None
    posthog_client._init_attempted = False
    yield
    posthog_client._client = None
    posthog_client._init_attempted = False


def _configure(monkeypatch, *, configured: bool):
    # posthog_configured is `bool(POSTHOG_API_KEY)`, so toggling the key on the
    # live settings instance flips it without touching the property.
    monkeypatch.setattr(
        posthog_client.settings,
        "POSTHOG_API_KEY",
        "phc_test_key" if configured else "",
    )


def test_get_client_builds_client_when_configured(monkeypatch):
    _configure(monkeypatch, configured=True)
    fake_instance = MagicMock(name="PosthogInstance")
    fake_posthog_cls = MagicMock(return_value=fake_instance)

    with patch.dict("sys.modules", {"posthog": MagicMock(Posthog=fake_posthog_cls)}):
        client = posthog_client._get_client()

    assert client is fake_instance
    fake_posthog_cls.assert_called_once()


def test_get_client_returns_none_when_unconfigured(monkeypatch):
    _configure(monkeypatch, configured=False)
    with patch.object(posthog_client.logger, "info") as mock_info:
        client = posthog_client._get_client()

    assert client is None
    mock_info.assert_called_once()


def test_get_client_swallows_init_failure(monkeypatch):
    _configure(monkeypatch, configured=True)
    exploding_cls = MagicMock(side_effect=RuntimeError("boom"))

    with patch.dict("sys.modules", {"posthog": MagicMock(Posthog=exploding_cls)}):
        with patch.object(posthog_client.logger, "warn") as mock_warn:
            client = posthog_client._get_client()

    assert client is None
    mock_warn.assert_called_once()


def test_get_client_is_attempted_only_once(monkeypatch):
    """A second call returns the memoised value without re-importing the SDK."""
    _configure(monkeypatch, configured=False)
    posthog_client._get_client()
    with patch.object(posthog_client.logger, "info") as mock_info:
        client = posthog_client._get_client()

    assert client is None
    mock_info.assert_not_called()  # already attempted, no second log


@pytest.mark.asyncio
async def test_capture_event_is_noop_when_client_none(monkeypatch):
    _configure(monkeypatch, configured=False)
    # Must not raise and must not try to send anything.
    await posthog_client.capture_event(
        distinct_id="uid-1", event="some_event", properties={"k": "v"}
    )


@pytest.mark.asyncio
async def test_capture_event_happy_path_calls_capture(monkeypatch):
    _configure(monkeypatch, configured=True)
    fake_instance = MagicMock(name="PosthogInstance")
    fake_posthog_cls = MagicMock(return_value=fake_instance)

    with patch.dict("sys.modules", {"posthog": MagicMock(Posthog=fake_posthog_cls)}):
        await posthog_client.capture_event(
            distinct_id="uid-1",
            event="signal_notification_sent",
            properties={"content_id": "c1"},
        )

    fake_instance.capture.assert_called_once_with(
        distinct_id="uid-1",
        event="signal_notification_sent",
        properties={"content_id": "c1"},
    )


@pytest.mark.asyncio
async def test_capture_event_swallows_send_failure(monkeypatch):
    _configure(monkeypatch, configured=True)
    fake_instance = MagicMock(name="PosthogInstance")
    fake_instance.capture.side_effect = RuntimeError("network down")
    fake_posthog_cls = MagicMock(return_value=fake_instance)

    with patch.dict("sys.modules", {"posthog": MagicMock(Posthog=fake_posthog_cls)}):
        with patch.object(posthog_client.logger, "warn") as mock_warn:
            # Must not propagate the capture failure.
            await posthog_client.capture_event(
                distinct_id="uid-1", event="e", properties={}
            )

    mock_warn.assert_called_once()


@pytest.mark.asyncio
async def test_flush_is_noop_when_never_initialised(monkeypatch):
    """No capture ever happened -> no client/queue to drain, and flush must not
    lazily build one just to flush nothing."""
    _configure(monkeypatch, configured=True)
    fake_posthog_cls = MagicMock()
    with patch.dict("sys.modules", {"posthog": MagicMock(Posthog=fake_posthog_cls)}):
        await posthog_client.flush()

    fake_posthog_cls.assert_not_called()


@pytest.mark.asyncio
async def test_flush_drains_existing_client():
    fake_instance = MagicMock(name="PosthogInstance")
    posthog_client._client = fake_instance
    posthog_client._init_attempted = True

    await posthog_client.flush()

    fake_instance.flush.assert_called_once()


@pytest.mark.asyncio
async def test_flush_swallows_failure():
    fake_instance = MagicMock(name="PosthogInstance")
    fake_instance.flush.side_effect = RuntimeError("drain failed")
    posthog_client._client = fake_instance
    posthog_client._init_attempted = True

    with patch.object(posthog_client.logger, "warn") as mock_warn:
        await posthog_client.flush()  # must not raise

    mock_warn.assert_called_once()
