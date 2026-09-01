import asyncio
import json

from src.handlers import meetings
from src.services.meetings import fields as F


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_meeting_response_includes_processing_contract():
    response = meetings._meeting_response({
        "meeting_id": "meeting-1",
        F.EVENT_ID: "event-1",
        F.TITLE: "Weekly sync",
        F.STATUS: F.STATUS_FAILED,
        F.NOTE: None,
        F.PROCESSING_STAGE: F.STAGE_BUILDING_INSIGHTS,
        F.FAILURE_CODE: F.FAIL_INSIGHT_GENERATION_FAILED,
        F.RETRYABLE: False,
        F.ATTEMPT_COUNT: 2,
        F.STATUS_REVISION: 4,
    })

    assert response["meeting_id"] == "meeting-1"
    assert response[F.STATUS] == F.STATUS_FAILED
    assert response[F.PROCESSING_STAGE] == F.STAGE_BUILDING_INSIGHTS
    assert response[F.FAILURE_CODE] == F.FAIL_INSIGHT_GENERATION_FAILED
    assert response[F.RETRYABLE] is False
    assert response[F.ATTEMPT_COUNT] == 2
    assert response[F.STATUS_REVISION] == 4


def test_detail_includes_transcript_and_recent_projection_omits_it():
    meeting = {
        "meeting_id": "meeting-1",
        F.STATUS: F.STATUS_READY,
        F.ARTIFACTS: {
            "canonical": {
                "path": "transcripts/v2/user-1/meeting-1/revisions/r1/canonical.json",
                "generation": "1",
                "sha256": "a" * 64,
            }
        },
        F.QUALITY_OUTCOME: "quality_passed",
        F.NOTE: {
            "summary": "Discussed the launch.",
            "decisions": ["Ship Friday"],
            "action_items": [],
            "open_questions": [],
            "language": "en",
            "one_sided": False,
            "partial": False,
            F.NOTE_TRANSCRIPT: [
                {F.TRANSCRIPT_SPEAKER: "You", F.TRANSCRIPT_TEXT: "Ship Friday?"},
                {F.TRANSCRIPT_SPEAKER: 2, F.TRANSCRIPT_TEXT: "Malformed"},
            ],
            "internal_future_field": "must not leak",
        },
    }

    detail = meetings._meeting_response(meeting)
    recent = meetings._meeting_response(meeting, include_transcript=False)

    assert detail[F.NOTE][F.NOTE_TRANSCRIPT] == [
        {F.TRANSCRIPT_SPEAKER: "You", F.TRANSCRIPT_TEXT: "Ship Friday?"},
    ]
    assert F.NOTE_TRANSCRIPT not in recent[F.NOTE]
    assert "internal_future_field" not in detail[F.NOTE]


def test_retry_rejects_ready_and_actively_synthesizing_meetings(monkeypatch):
    # Auth moved behind handlers/request_guards.require_user; patch that seam.
    monkeypatch.setattr(meetings, "require_user", lambda request: "user-1")
    monkeypatch.setattr(meetings, "resolve_user_id_from_request", lambda request: "user-1")

    async def run(meeting_doc: dict):
        async def get_meeting(uid, meeting_id):
            return meeting_doc

        monkeypatch.setattr(meetings.store, "get_meeting", get_meeting)
        return await meetings.handle_retry(object(), "meeting-1")

    ready = asyncio.run(run({F.STATUS: F.STATUS_READY}))
    active = asyncio.run(run({F.STATUS: F.STATUS_SYNTHESIZING}))

    assert ready.status_code == 409
    assert active.status_code == 409


def test_retry_dispatches_the_current_v2_job(monkeypatch):
    # Auth moved behind handlers/request_guards.require_user; patch that seam.
    monkeypatch.setattr(meetings, "require_user", lambda request: "user-1")
    monkeypatch.setattr(meetings, "resolve_user_id_from_request", lambda request: "user-1")

    async def get_meeting(uid, meeting_id):
        return {
            F.STATUS: F.STATUS_FAILED,
            F.ATTEMPT_COUNT: 3,
        }

    calls: list[tuple[str, str]] = []

    async def retry_v2_job(uid, meeting_id):
        return "meeting:job-4"

    async def dispatch_job(uid, job_id):
        calls.append((uid, job_id))

    monkeypatch.setattr(meetings.store, "get_meeting", get_meeting)
    monkeypatch.setattr(meetings.store, "retry_v2_job", retry_v2_job)
    monkeypatch.setattr(meetings.tasks, "dispatch_job", dispatch_job)

    response = asyncio.run(meetings.handle_retry(object(), "meeting-1"))

    assert response.status_code == 200
    assert _body(response)["status"] == F.STATUS_UPLOADED
    assert calls == [("user-1", "meeting:job-4")]
