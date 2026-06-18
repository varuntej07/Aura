"""
Tests for the silent product-feedback capture path (Buddy's report_feedback tool).

Covers the tool/schema contract (so a rename on either side breaks CI instead of silently
flattening the feature), the FeedbackReport coercion, the persistence + Telegram orchestration, and
the fail-safe behaviour (never raises, no-op when unconfigured).
"""

from __future__ import annotations

import asyncio

from src.config.settings import settings
from src.services.feedback import feedback_capture, telegram_client
from src.services.feedback.feedback_schema import (
    FEEDBACK_ABOUT_AREAS,
    FEEDBACK_CATEGORIES,
    FEEDBACK_COLLECTION,
    FEEDBACK_SEVERITIES,
    FEEDBACK_TOOL_NAME,
    FIELD_ABOUT,
    FIELD_CATEGORY,
    FIELD_COUNTRY,
    FIELD_LOCAL_TIME,
    FIELD_QUOTE,
    FIELD_REGION,
    FIELD_SEVERITY,
    FIELD_SOURCE,
    FIELD_STATUS,
    FIELD_SUMMARY,
    FIELD_TIMEZONE,
    FIELD_UID,
    FIELD_USERNAME,
    STATUS_NEW,
    FeedbackReport,
    FeedbackUserContext,
    build_feedback_document,
)
from src.shared.tools import claude_tool_definitions


def _report_feedback_spec() -> dict | None:
    return next(
        (t for t in claude_tool_definitions() if t["name"] == FEEDBACK_TOOL_NAME),
        None,
    )


# --- Tool / schema contract (anti-drift) ---------------------------------


def test_tool_always_offered_to_chat():
    # No flag: report_feedback is offered to the chat model for every user, always.
    assert _report_feedback_spec() is not None


def test_tool_enums_match_taxonomy():
    spec = _report_feedback_spec()
    assert spec is not None
    props = spec["input_schema"]["properties"]
    assert props["category"]["enum"] == FEEDBACK_CATEGORIES
    assert props["about"]["enum"] == FEEDBACK_ABOUT_AREAS
    assert props["severity"]["enum"] == FEEDBACK_SEVERITIES


# --- FeedbackReport coercion ---------------------------------------------


def test_report_coerces_offlist_values_to_safe_defaults():
    report = FeedbackReport(
        category="totally_made_up",
        about="nowhere",
        severity="catastrophic",
        summary="  user is annoyed  ",
        verbatim_quote="this is dumb",
    )
    assert report.category == "other"
    assert report.about == "general"
    assert report.severity == "medium"
    assert report.summary == "user is annoyed"  # stripped


def test_report_keeps_valid_values_and_caps_length():
    report = FeedbackReport(
        category="complaint",
        about="notifications",
        severity="high",
        summary="x" * 1000,
        verbatim_quote="y" * 1000,
    )
    assert report.category == "complaint"
    assert report.about == "notifications"
    assert report.severity == "high"
    assert len(report.summary) <= 280
    assert len(report.verbatim_quote) <= 500


def test_build_document_has_all_fields():
    report = FeedbackReport(
        category="feature_request",
        about="notifications",
        summary="wants Belgium-only football updates",
        verbatim_quote="only send me events about Belgium playing",
        severity="low",
    )
    doc = build_feedback_document("uid-123", report, source="text", session_id="sess-1")
    assert doc[FIELD_UID] == "uid-123"
    assert doc[FIELD_CATEGORY] == "feature_request"
    assert doc[FIELD_ABOUT] == "notifications"
    assert doc[FIELD_SUMMARY] == "wants Belgium-only football updates"
    assert doc[FIELD_QUOTE] == "only send me events about Belgium playing"
    assert doc[FIELD_SEVERITY] == "low"
    assert doc[FIELD_SOURCE] == "text"
    assert doc[FIELD_STATUS] == STATUS_NEW


def test_build_document_carries_user_context():
    report = FeedbackReport(category="praise", about="voice", summary="loves it", verbatim_quote="great")
    context = FeedbackUserContext(
        username="Varun",
        timezone="Asia/Kolkata",
        local_time="2026-06-16 14:32",
        region="IN",
        country="India",
    )
    doc = build_feedback_document("uid-7", report, source="voice", session_id=None, context=context)
    assert doc[FIELD_USERNAME] == "Varun"
    assert doc[FIELD_TIMEZONE] == "Asia/Kolkata"
    assert doc[FIELD_LOCAL_TIME] == "2026-06-16 14:32"
    assert doc[FIELD_REGION] == "IN"
    assert doc[FIELD_COUNTRY] == "India"


def test_build_document_defaults_context_to_empty():
    # No context (e.g. profile read failed) → enrichment fields present but empty, never missing.
    report = FeedbackReport(category="bug", about="chat", summary="broke", verbatim_quote="it broke")
    doc = build_feedback_document("uid-7", report, source="text", session_id=None)
    assert doc[FIELD_USERNAME] == ""
    assert doc[FIELD_REGION] == ""
    assert doc[FIELD_COUNTRY] == ""


# --- capture_feedback orchestration --------------------------------------


class _FakeDoc:
    def __init__(self, sink: dict):
        self._sink = sink

    def set(self, data):
        self._sink["doc"] = data


class _FakeCollection:
    def __init__(self, sink: dict):
        self._sink = sink

    def document(self, doc_id: str | None = None):
        del doc_id  # signature parity with firestore's .document(id); the fake ignores the id
        return _FakeDoc(self._sink)


class _FakeDB:
    def __init__(self, sink: dict):
        self._sink = sink

    def collection(self, name: str):
        self._sink["collection"] = name
        return _FakeCollection(self._sink)


async def test_capture_persists_and_schedules_alert(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr("src.services.firebase.admin_firestore", lambda: _FakeDB(sink))

    alerts: list[str] = []

    async def _fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(feedback_capture, "send_feedback_alert", _fake_alert)

    report = FeedbackReport(
        category="complaint",
        about="notifications",
        summary="disliked a notification",
        verbatim_quote="why did I get this, I don't like it",
        severity="medium",
    )
    await feedback_capture.capture_feedback("uid-9", report, source="voice", session_id=None)
    await asyncio.sleep(0)  # let the detached alert task run

    assert sink["collection"] == FEEDBACK_COLLECTION
    assert sink["doc"][FIELD_UID] == "uid-9"
    assert sink["doc"][FIELD_CATEGORY] == "complaint"
    assert sink["doc"][FIELD_SOURCE] == "voice"
    assert len(alerts) == 1
    assert "why did I get this" in alerts[0]


class _ProfileSnap:
    def __init__(self, data: dict | None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _ProfileDocRef:
    def __init__(self, data: dict | None):
        self._data = data

    def get(self):
        return _ProfileSnap(self._data)


class _EnrichDB:
    """Fake DB that serves the users/{uid} profile read AND the observed_feedback write."""

    def __init__(self, sink: dict, profile: dict | None):
        self._sink = sink
        self._profile = profile

    def collection(self, name: str):
        if name == "users":
            return _UsersCollection(self._profile)
        self._sink["collection"] = name
        return _FakeCollection(self._sink)


class _UsersCollection:
    def __init__(self, profile: dict | None):
        self._profile = profile

    def document(self, doc_id: str | None = None):
        del doc_id  # signature parity with firestore's .document(id); the fake ignores the id
        return _ProfileDocRef(self._profile)


async def test_capture_enriches_alert_and_doc_from_profile(monkeypatch):
    sink: dict = {}
    profile = {"display_name": "Varun", "timezone": "Asia/Kolkata"}
    monkeypatch.setattr(
        "src.services.firebase.admin_firestore", lambda: _EnrichDB(sink, profile)
    )

    alerts: list[str] = []

    async def _fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(feedback_capture, "send_feedback_alert", _fake_alert)

    report = FeedbackReport(
        category="feature_request",
        about="voice",
        summary="wants a calmer voice",
        verbatim_quote="can the voice be softer",
        severity="low",
    )
    await feedback_capture.capture_feedback("uid-42", report, source="voice", session_id=None)
    await asyncio.sleep(0)

    doc = sink["doc"]
    assert doc[FIELD_USERNAME] == "Varun"
    assert doc[FIELD_TIMEZONE] == "Asia/Kolkata"
    assert doc[FIELD_REGION] == "IN"
    assert doc[FIELD_COUNTRY] == "India"
    assert doc[FIELD_LOCAL_TIME]  # derived, non-empty

    assert len(alerts) == 1
    assert "Varun" in alerts[0]
    assert "India (IN)" in alerts[0]
    assert "Asia/Kolkata" in alerts[0]


async def test_capture_enrichment_failure_still_persists_and_pings(monkeypatch):
    # A profile read that explodes must not lose the capture — doc + alert still go out, unenriched.
    sink: dict = {}

    class _ProfileBoomDB:
        def collection(self, name: str):
            if name == "users":
                raise RuntimeError("profile read down")
            sink["collection"] = name
            return _FakeCollection(sink)

    monkeypatch.setattr("src.services.firebase.admin_firestore", lambda: _ProfileBoomDB())

    alerts: list[str] = []

    async def _fake_alert(text: str) -> None:
        alerts.append(text)

    monkeypatch.setattr(feedback_capture, "send_feedback_alert", _fake_alert)

    report = FeedbackReport(category="bug", about="chat", summary="broke", verbatim_quote="it broke")
    await feedback_capture.capture_feedback("uid-1", report, source="text", session_id=None)
    await asyncio.sleep(0)

    assert sink["doc"][FIELD_UID] == "uid-1"
    assert sink["doc"][FIELD_USERNAME] == ""  # enrichment skipped, field still present
    assert len(alerts) == 1


async def test_capture_never_raises_on_firestore_error(monkeypatch):
    def _boom():
        raise RuntimeError("firestore down")

    monkeypatch.setattr("src.services.firebase.admin_firestore", _boom)

    called: list[str] = []

    async def _fake_alert(text: str) -> None:
        called.append(text)

    monkeypatch.setattr(feedback_capture, "send_feedback_alert", _fake_alert)

    report = FeedbackReport(category="bug", about="chat", summary="broke", verbatim_quote="it broke")
    # Must not raise — a capture failure can never break a chat/voice turn.
    await feedback_capture.capture_feedback("uid-1", report, source="text", session_id=None)
    await asyncio.sleep(0)
    # Alert is only scheduled after a successful write, so a failed write means no ping.
    assert called == []


# --- Telegram transport fail-safe ----------------------------------------


async def test_telegram_alert_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_FEEDBACK_CHAT_ID", "")

    def _explode(*args, **kwargs):
        del args, kwargs
        raise AssertionError("httpx must not be touched when Telegram is unconfigured")

    monkeypatch.setattr(telegram_client.httpx, "AsyncClient", _explode)
    # No exception, no network.
    await telegram_client.send_feedback_alert("hello")


async def test_telegram_alert_posts_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "BOTTOKEN")
    monkeypatch.setattr(settings, "TELEGRAM_FEEDBACK_CHAT_ID", "12345")

    sent: dict = {}

    class _FakeResp:
        status_code = 200
        text = "ok"

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            del exc
            return False

        async def post(self, url, json):
            sent["url"] = url
            sent["json"] = json
            return _FakeResp()

    monkeypatch.setattr(telegram_client.httpx, "AsyncClient", _FakeClient)

    await telegram_client.send_feedback_alert("a feedback line")

    assert "BOTTOKEN/sendMessage" in sent["url"]
    assert sent["json"]["chat_id"] == "12345"
    assert sent["json"]["text"] == "a feedback line"
