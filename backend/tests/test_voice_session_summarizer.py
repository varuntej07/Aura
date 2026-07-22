"""
Tests for src/services/voice_session_summarizer.py

Covers:
  - run_post_session_pipeline fault isolation (each step fails independently)
  - Archive accumulation: existing archive included in re-synthesis
  - _generate_session_summary edge cases
  - _synthesize_archive with and without prior archive
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_turns(n: int = 3) -> list[dict]:
    return [
        {"role": "user", "text": f"turn {i}", "timestamp": "2026-01-01T00:00:00Z"}
        for i in range(n)
    ]


def _make_firestore_mock(
    *,
    count_value: int = 5,
    existing_archive: str = "",
) -> MagicMock:
    """Return a mock admin_firestore() client that satisfies all pipeline queries."""
    db = MagicMock()

    # Collection chain: db.collection().document().collection().document().get()
    # and .collection().document().collection().count().get()
    # and .collection().document().collection().where().order_by().limit().stream()

    # Build a flexible mock that returns sensible defaults at every chain point
    doc_ref = MagicMock()
    doc_ref.set = MagicMock()
    doc_ref.update = MagicMock()
    doc_ref.delete = MagicMock()

    # Mock for voice_session_state/latest and voice_session_state/archive get()
    latest_snapshot = MagicMock()
    latest_snapshot.to_dict.return_value = {}

    archive_snapshot = MagicMock()
    archive_snapshot.to_dict.return_value = (
        {"archive_summary": existing_archive} if existing_archive else {}
    )

    # count aggregate result: result[0][0].value
    count_agg = MagicMock()
    count_agg.value = count_value
    count_result = [[count_agg]]

    # query chain: .where().count().get()
    query_mock = MagicMock()
    query_mock.count.return_value.get.return_value = count_result
    # query chain for fetch oldest: .where().order_by().limit().stream()
    query_mock.order_by.return_value.limit.return_value.stream.return_value = iter([])

    coll_mock = MagicMock()
    coll_mock.where.return_value = query_mock
    coll_mock.document.return_value = doc_ref

    doc_ref.collection.return_value = coll_mock

    # voice_session_state subcollection document selectors
    def _state_doc(doc_id: str) -> MagicMock:
        if doc_id == "latest":
            m = MagicMock()
            m.get.return_value = latest_snapshot
            m.set = MagicMock()
            return m
        if doc_id == "archive":
            m = MagicMock()
            m.get.return_value = archive_snapshot
            m.set = MagicMock()
            return m
        return doc_ref

    state_coll = MagicMock()
    state_coll.document.side_effect = _state_doc

    def _subcoll(name: str) -> MagicMock:
        if name == "voice_session_state":
            return state_coll
        return coll_mock

    top_doc = MagicMock()
    top_doc.collection.side_effect = _subcoll

    users_coll = MagicMock()
    users_coll.document.return_value = top_doc

    batch_mock = MagicMock()
    batch_mock.set = MagicMock()
    batch_mock.update = MagicMock()
    batch_mock.commit = MagicMock()

    db.collection.return_value = users_coll
    db.batch.return_value = batch_mock

    return db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_model_provider():
    from src.services.voice_session_summarizer import VoiceSessionMemory

    provider = MagicMock()
    provider.cheap = AsyncMock(return_value=VoiceSessionMemory(recap="session summary text"))
    with patch("src.services.voice_session_summarizer.get_model_provider", return_value=provider):
        yield provider


@pytest.fixture()
def mock_firestore_factory():
    """Returns a factory: call with kwargs to configure the mock db."""
    def _factory(**kwargs) -> MagicMock:
        db = _make_firestore_mock(**kwargs)
        return db
    return _factory


# ---------------------------------------------------------------------------
# _generate_session_summary
# ---------------------------------------------------------------------------

class TestGenerateSessionSummary:
    @pytest.mark.asyncio
    async def test_returns_empty_string_for_empty_turns(self, mock_model_provider):
        from src.services.voice_session_summarizer import _generate_session_summary
        result = await _generate_session_summary([])
        assert result.recap == ""
        mock_model_provider.cheap.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_text_field(self, mock_model_provider):
        from src.services.voice_session_summarizer import _generate_session_summary
        turns = [{"role": "user", "timestamp": "2026-01-01T00:00:00Z"}]
        result = await _generate_session_summary(turns)
        assert result.recap == ""
        mock_model_provider.cheap.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_calls_gemini_and_returns_summary(self, mock_model_provider):
        from src.services.voice_session_summarizer import (
            VoiceSessionMemory,
            _generate_session_summary,
        )

        mock_model_provider.cheap.return_value = VoiceSessionMemory(
            recap="Talked about work.", open_loops=["Finish the report"],
        )
        result = await _generate_session_summary(_make_turns(2))
        assert result.recap == "Talked about work."
        assert result.open_loops == ["Finish the report"]
        assert mock_model_provider.cheap.call_args.kwargs["response_model"] is VoiceSessionMemory
        mock_model_provider.cheap.assert_called_once()


@pytest.mark.asyncio
async def test_schema_v2_writer_reader_round_trip_uses_shared_field_contract():
    from src.handlers.history import _session_summary_row
    from src.services.voice_session_summarizer import VoiceSessionMemory, _write_session_doc

    db = MagicMock()
    session_ref = MagicMock()
    (
        db.collection.return_value.document.return_value
        .collection.return_value.document
    ).return_value = session_ref
    memory = VoiceSessionMemory(
        recap="We planned a call with Mom.",
        open_loops=["Call Mom tomorrow"],
        facts=["Mom lives in Chicago"],
    )
    receipts = [
        {"tool_name": "set_reminder", "call_id": "c1", "success": True,
         "occurred_at": "2026-07-14T20:00:00Z"},
        {"tool_name": "create_calendar_event", "call_id": "c2", "success": False,
         "occurred_at": "2026-07-14T20:00:01Z"},
    ]

    with patch("src.services.voice_session_summarizer.admin_firestore", return_value=db):
        await _write_session_doc(
            "u1", "run-1", memory, _make_turns(1),
            "start", "end", 1000, ["set_reminder"], 0,
            conversation_id="conversation-1",
            surface="app",
            action_receipts=receipts,
        )

    payload = session_ref.set.call_args.args[0]
    row = _session_summary_row("run-1", payload)
    assert row["voice_run_id"] == "run-1"
    assert row["conversation_id"] == "conversation-1"
    assert row["surface"] == "app"
    assert row["schema_version"] == 2
    assert row["recap"] == "We planned a call with Mom."
    assert [action["tool_name"] for action in row["actions"]] == ["set_reminder"]


# ---------------------------------------------------------------------------
# _synthesize_archive
# ---------------------------------------------------------------------------

class TestSynthesizeArchive:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_valid_summaries(self, mock_model_provider):
        from src.services.voice_session_summarizer import _synthesize_archive
        result = await _synthesize_archive([{"doc_id": "x", "summary": ""}])
        assert result == ""
        mock_model_provider.cheap.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_existing_archive_has_no_prior_section(self, mock_model_provider):
        from src.services.voice_session_summarizer import _synthesize_archive
        mock_model_provider.cheap.return_value = "archive result"
        await _synthesize_archive(
            [{"doc_id": "x", "summary": "user talked about work"}],
            existing_archive="",
        )
        prompt_arg = mock_model_provider.cheap.call_args[0][0]
        assert "PRIOR ARCHIVE" not in prompt_arg

    @pytest.mark.asyncio
    async def test_existing_archive_included_in_prompt(self, mock_model_provider):
        from src.services.voice_session_summarizer import _synthesize_archive
        mock_model_provider.cheap.return_value = "new archive result"
        await _synthesize_archive(
            [{"doc_id": "x", "summary": "user talked about running"}],
            existing_archive="LIFE FACTS\nuser is a developer",
        )
        prompt_arg = mock_model_provider.cheap.call_args[0][0]
        assert "PRIOR ARCHIVE" in prompt_arg
        assert "user is a developer" in prompt_arg


# ---------------------------------------------------------------------------
# run_post_session_pipeline — fault isolation
# ---------------------------------------------------------------------------

_PIPELINE_ARGS = dict(
    user_id="uid28chars1234567890123456",
    session_id="session-id-1",
    conversation_id="conversation-id-1",
    surface="app",
    turns=_make_turns(3),
    started_at="2026-01-01T10:00:00Z",
    ended_at="2026-01-01T10:10:00Z",
    duration_ms=600_000,
    tool_calls=["set_reminder"],
    action_receipts=[],
)


class TestRunPostSessionPipelineFaultIsolation:
    @pytest.mark.asyncio
    async def test_pipeline_does_not_raise_when_gemini_fails(
        self, mock_model_provider, mock_firestore_factory
    ):
        """Gemini down → summary="" but writes still attempted."""
        mock_model_provider.cheap.side_effect = Exception("Gemini 503")
        db = mock_firestore_factory(count_value=5)
        with patch("src.services.voice_session_summarizer.admin_firestore", return_value=db):
            from src.services.voice_session_summarizer import run_post_session_pipeline
            # Must not raise
            await run_post_session_pipeline(**_PIPELINE_ARGS)

    @pytest.mark.asyncio
    async def test_pipeline_does_not_raise_when_session_count_fails(
        self, mock_model_provider, mock_firestore_factory
    ):
        """Firestore count() fails → session_count=0, archive never triggers."""
        db = mock_firestore_factory(count_value=5)
        # Make count() raise
        db.collection.return_value.document.return_value.collection.return_value.where.return_value.count.return_value.get.side_effect = Exception("Firestore unavailable")
        with patch("src.services.voice_session_summarizer.admin_firestore", return_value=db):
            from src.services.voice_session_summarizer import run_post_session_pipeline
            await run_post_session_pipeline(**_PIPELINE_ARGS)

    @pytest.mark.asyncio
    async def test_pipeline_does_not_raise_when_doc_write_fails(
        self, mock_model_provider, mock_firestore_factory
    ):
        """Session doc write fails → logged, latest write still attempted."""
        db = mock_firestore_factory(count_value=5)
        with patch("src.services.voice_session_summarizer.admin_firestore", return_value=db):
            # Patch _write_session_doc to raise
            with patch(
                "src.services.voice_session_summarizer._write_session_doc",
                new_callable=AsyncMock,
                side_effect=Exception("Firestore write failed"),
            ):
                from src.services.voice_session_summarizer import run_post_session_pipeline
                await run_post_session_pipeline(**_PIPELINE_ARGS)

    @pytest.mark.asyncio
    async def test_archive_not_triggered_below_threshold(
        self, mock_model_provider, mock_firestore_factory
    ):
        """count <= 30 → archive synthesis never called."""
        db = mock_firestore_factory(count_value=25)
        with patch("src.services.voice_session_summarizer.admin_firestore", return_value=db):
            with patch(
                "src.services.voice_session_summarizer._synthesize_archive",
                new_callable=AsyncMock,
            ) as mock_synthesize:
                from src.services.voice_session_summarizer import run_post_session_pipeline
                await run_post_session_pipeline(**_PIPELINE_ARGS)
                mock_synthesize.assert_not_called()

    @pytest.mark.asyncio
    async def test_archive_cycle_includes_existing_archive_text(
        self, mock_model_provider, mock_firestore_factory
    ):
        """count > 30 and existing archive exists → prior archive passed to synthesis."""
        prior_archive = "LIFE FACTS\nuser is a developer in Hyderabad"
        oldest_sessions = [
            {"doc_id": "old-1", "summary": "user talked about deadlines", "started_at": "2026-01-01T09:00:00Z"},
        ]
        db = mock_firestore_factory(count_value=35)

        with patch("src.services.voice_session_summarizer.admin_firestore", return_value=db):
            with patch(
                "src.services.voice_session_summarizer._count_active_sessions",
                new_callable=AsyncMock, return_value=35,
            ):
                with patch(
                    "src.services.voice_session_summarizer._generate_session_summary",
                    new_callable=AsyncMock, return_value="summary",
                ):
                    with patch(
                        "src.services.voice_session_summarizer._fetch_oldest_active_summaries",
                        new_callable=AsyncMock, return_value=oldest_sessions,
                    ):
                        with patch(
                            "src.services.voice_session_summarizer._fetch_existing_archive_text",
                            new_callable=AsyncMock, return_value=prior_archive,
                        ):
                            with patch(
                                "src.services.voice_session_summarizer._synthesize_archive",
                                new_callable=AsyncMock, return_value="synthesized",
                            ) as mock_synth:
                                with patch(
                                    "src.services.voice_session_summarizer._archive_sessions",
                                    new_callable=AsyncMock,
                                ):
                                    from src.services.voice_session_summarizer import (
                                        run_post_session_pipeline,
                                    )
                                    await run_post_session_pipeline(**_PIPELINE_ARGS)
                                    mock_synth.assert_called_once()
                                    call_args = mock_synth.call_args
                                    # existing_archive should be passed as second positional or keyword arg
                                    passed_archive = (
                                        call_args.kwargs.get("existing_archive")
                                        or (call_args.args[1] if len(call_args.args) > 1 else None)
                                    )
                                    assert passed_archive == prior_archive

    @pytest.mark.asyncio
    async def test_archive_not_triggered_when_synthesis_returns_empty(
        self, mock_model_provider, mock_firestore_factory
    ):
        """Synthesis returns "" → _archive_sessions never called."""
        db = mock_firestore_factory(count_value=35)

        oldest_doc = MagicMock()
        oldest_doc.id = "old-1"
        oldest_doc.to_dict.return_value = {"summary": "", "started_at": "2026-01-01T09:00:00Z"}
        (
            db.collection.return_value.document.return_value
            .collection.return_value.where.return_value
            .order_by.return_value.limit.return_value.stream
        ).return_value = iter([oldest_doc])

        with patch("src.services.voice_session_summarizer.admin_firestore", return_value=db):
            with patch(
                "src.services.voice_session_summarizer._archive_sessions",
                new_callable=AsyncMock,
            ) as mock_archive:
                from src.services.voice_session_summarizer import run_post_session_pipeline
                await run_post_session_pipeline(**_PIPELINE_ARGS)
                mock_archive.assert_not_called()
