"""Regression tests for reminder create-time de-duplication.

The bug (confirmed against real prod data): the model creates the SAME task
twice in one session or on an edit-and-resend replay, sometimes with reworded
text ("Send a DM to Vish Jaggi about Lululemon" vs "Send a DM to Vishal about
Lululemon") and the same or a nearby fire time. The old guard matched only on
EXACT message text, so paraphrases slipped through and the user got buzzed twice.

The fix is a two-layer create-time guard over pending reminders within a 3h fire
window: (1) exact text, then (2) a conservative embedding-similarity check that
catches paraphrases without merging a legitimate batch of DISTINCT tasks set at
one time. It fails open if the embedding API errors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import src.services.analytics.posthog_client as posthog_client
import src.services.signal_engine.embedder as embedder_module
import src.services.threads.thread_writer as thread_writer
from src.services.tool_executor import (
    ToolExecutor,
    _cosine,
    _within_trigger_window,
)


# ── Fake reminders collection (create path filters status==pending and writes
#    via document(id).set(...)) ─────────────────────────────────────────────────
class _FakeDoc:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


class _FakeDocRef:
    def __init__(self, store: dict, doc_id: str):
        self._store = store
        self.id = doc_id

    def set(self, data: dict) -> None:
        self._store[self.id] = dict(data)


class _FakeQuery:
    def __init__(self, store: dict):
        self._store = store

    def where(self, *, filter=None):  # noqa: A002 - mirrors Firestore's kwarg name
        return self

    def stream(self):
        return [
            _FakeDoc(k, v)
            for k, v in self._store.items()
            if v.get("status") == "pending"
        ]


class _FakeCollection:
    def __init__(self, store: dict):
        self._store = store

    def where(self, *, filter=None):  # noqa: A002 - mirrors Firestore's kwarg name
        return _FakeQuery(self._store)

    def document(self, doc_id: str):
        return _FakeDocRef(self._store, doc_id)


def _executor_with_store(monkeypatch) -> tuple[ToolExecutor, dict]:
    store: dict = {}
    ex = ToolExecutor("u1", created_via="text")
    monkeypatch.setattr(ex, "_reminders_ref", lambda: _FakeCollection(store))
    monkeypatch.setattr(thread_writer, "record_reminder_thread", AsyncMock())
    monkeypatch.setattr(posthog_client, "capture_event", AsyncMock())
    return ex, store


def _patch_embed(monkeypatch, vectors=None, *, error=None) -> AsyncMock:
    mock = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=vectors)
    monkeypatch.setattr(embedder_module, "embed_texts", mock)
    return mock


# ── pure-helper unit coverage ────────────────────────────────────────────────
def test_cosine_identical_orthogonal_and_zero():
    assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector is guarded


def test_within_trigger_window():
    t = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
    assert _within_trigger_window((t + timedelta(hours=2)).isoformat(), t) is True
    assert _within_trigger_window((t + timedelta(hours=4)).isoformat(), t) is False
    assert _within_trigger_window(None, t) is False
    assert _within_trigger_window("not-a-date", t) is False
    # legacy naive value is read as UTC, not rejected
    assert _within_trigger_window(t.replace(tzinfo=None).isoformat(), t) is True


# ── create-time integration ──────────────────────────────────────────────────
async def test_exact_duplicate_within_window_skips_embedding(monkeypatch):
    ex, store = _executor_with_store(monkeypatch)
    embed = _patch_embed(monkeypatch, [[1.0, 0.0], [1.0, 0.0]])
    base = datetime.now(UTC) + timedelta(hours=1)

    first = await ex._set_reminder({"message": "Call mom", "scheduled_at": base.isoformat()})
    second = await ex._set_reminder(
        {"message": "call mom", "scheduled_at": (base + timedelta(seconds=40)).isoformat()}
    )

    assert len(store) == 1
    assert second["reminder_id"] == first["reminder_id"]
    embed.assert_not_awaited()  # exact match short-circuits before any embed call


async def test_paraphrase_within_window_is_deduped(monkeypatch):
    ex, store = _executor_with_store(monkeypatch)
    _patch_embed(monkeypatch, [[1.0, 0.0], [0.99, 0.02]])  # cosine ~1.0 >= threshold
    base = datetime.now(UTC) + timedelta(hours=2)

    first = await ex._set_reminder(
        {"message": "Send a LinkedIn DM to Vish Jaggi about Lululemon", "scheduled_at": base.isoformat()}
    )
    second = await ex._set_reminder(
        {"message": "Send a DM to Vishal about Lululemon", "scheduled_at": base.isoformat()}
    )

    assert len(store) == 1, "a re-worded duplicate of the same task must collapse to one"
    assert second["reminder_id"] == first["reminder_id"]


async def test_distinct_tasks_at_same_time_are_kept(monkeypatch):
    ex, store = _executor_with_store(monkeypatch)
    _patch_embed(monkeypatch, [[1.0, 0.0], [0.0, 1.0]])  # cosine 0.0 < threshold
    base = datetime.now(UTC) + timedelta(hours=3)

    await ex._set_reminder({"message": "Ask gender at onboarding", "scheduled_at": base.isoformat()})
    await ex._set_reminder(
        {"message": "Let users pick what voice they want", "scheduled_at": base.isoformat()}
    )

    assert len(store) == 2, "a batch of distinct tasks at one time must survive"


async def test_similar_text_far_apart_in_time_is_kept(monkeypatch):
    ex, store = _executor_with_store(monkeypatch)
    embed = _patch_embed(monkeypatch, [[1.0, 0.0], [1.0, 0.0]])
    base = datetime.now(UTC) + timedelta(hours=1)

    first = await ex._set_reminder({"message": "Fill out I-983 form", "scheduled_at": base.isoformat()})
    # 4 hours later is outside the window: an intentional re-set, not a duplicate.
    second = await ex._set_reminder(
        {"message": "Fill out I-983 form", "scheduled_at": (base + timedelta(hours=4)).isoformat()}
    )

    assert len(store) == 2
    assert second["reminder_id"] != first["reminder_id"]
    embed.assert_not_awaited()  # no candidate in the window, so nothing to embed


async def test_embedding_failure_fails_open(monkeypatch):
    ex, store = _executor_with_store(monkeypatch)
    _patch_embed(monkeypatch, error=RuntimeError("quota exhausted"))
    base = datetime.now(UTC) + timedelta(hours=2)

    await ex._set_reminder(
        {"message": "Hook up Indeed and Apify connectors", "scheduled_at": base.isoformat()}
    )
    await ex._set_reminder(
        {"message": "Wire Indeed plus Apify integrations", "scheduled_at": base.isoformat()}
    )

    # Embedding is the only way to catch this paraphrase; if it errors we must
    # still create the reminder rather than silently drop the user's request.
    assert len(store) == 2
