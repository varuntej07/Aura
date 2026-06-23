"""Coverage for the dormancy win-back producer.

Pins: the idle cohort is exactly active(6d) - active(5d); a win-back is enqueued as a
PROACTIVE re-engage proposal through the funnel; it stands down at the user's night and
when a recent win-back is still within cooldown; and the framer always yields warm copy
(personalised on consent, generic otherwise) even when the LLM is down.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.services.notification_service import NotificationResult
from src.services.notifications.proposal import SOURCE_REENGAGE, ProposalKind
from src.services.reengagement import reengagement_engine as rx
from src.services.reengagement import reengagement_store as store


def test_parse_valid_json():
    raw = '{"title":"Hi","body":"come back","opening_chat_message":"hey"}'
    assert rx._parse(raw, "") == ("Hi", "come back", "hey")


def test_parse_falls_back_personalised_then_generic():
    # Garbage → personalised fallback nods to the interest...
    _, body, _ = rx._parse("not json", "cricket")
    assert "cricket" in body
    # ...and a generic warm fallback when there's no interest.
    title, body, opening = rx._parse("{bad", "")
    assert title and body and opening
    assert "cricket" not in body


async def test_frame_falls_back_when_llm_errors():
    models = MagicMock()
    models.cheap = AsyncMock(side_effect=RuntimeError("framer down"))
    _, body, _ = await rx._frame(models, "cricket")
    assert "cricket" in body  # warm fallback, never an empty/blocked send


async def test_cohort_is_active6_minus_active5(monkeypatch):
    async def _fake_active(days):
        return {6: ["a", "b", "c"], 5: ["a"]}[days]

    monkeypatch.setattr(rx, "list_active_user_ids", _fake_active)
    assert await rx._dormant_cohort() == {"b", "c"}


async def test_reengage_one_skips_quiet_hours_without_claiming(monkeypatch):
    summary = rx.ReengageTickSummary()
    monkeypatch.setattr(
        rx.store, "read_targeting",
        AsyncMock(return_value=store.ReengageTargeting(timezone="UTC")),
    )
    monkeypatch.setattr(rx, "is_within_active_hours", lambda *a, **k: False)
    claim = AsyncMock()
    submit = AsyncMock()
    monkeypatch.setattr(rx.store, "claim_reengagement", claim)
    monkeypatch.setattr(rx.orchestrator, "submit", submit)

    await rx._reengage_one("u1", MagicMock(), summary)

    assert summary.skipped_quiet_hours == 1
    claim.assert_not_awaited()  # never even claim at the user's night
    submit.assert_not_awaited()


async def test_reengage_one_skips_when_recently_claimed(monkeypatch):
    summary = rx.ReengageTickSummary()
    monkeypatch.setattr(
        rx.store, "read_targeting",
        AsyncMock(return_value=store.ReengageTargeting(timezone="UTC")),
    )
    monkeypatch.setattr(rx, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(rx.store, "claim_reengagement", AsyncMock(return_value=False))
    submit = AsyncMock()
    monkeypatch.setattr(rx.orchestrator, "submit", submit)

    await rx._reengage_one("u1", MagicMock(), summary)

    assert summary.skipped_claimed == 1
    submit.assert_not_awaited()


async def test_reengage_one_enqueues_proactive_proposal(monkeypatch):
    summary = rx.ReengageTickSummary()
    monkeypatch.setattr(
        rx.store, "read_targeting",
        AsyncMock(return_value=store.ReengageTargeting(
            consent_granted=True, timezone="UTC", top_interest="cricket",
        )),
    )
    monkeypatch.setattr(rx, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(rx.store, "claim_reengagement", AsyncMock(return_value=True))
    monkeypatch.setattr(rx, "_frame", AsyncMock(return_value=("t", "b", "o")))
    submit = AsyncMock()
    monkeypatch.setattr(rx.orchestrator, "submit", submit)

    await rx._reengage_one("u1", MagicMock(), summary)

    assert summary.enqueued == 1
    submit.assert_awaited_once()
    proposal = submit.await_args.args[0]
    assert proposal.source == SOURCE_REENGAGE
    assert proposal.kind == ProposalKind.PROACTIVE
    assert proposal.dedup_key == "reengage_u1"
    assert proposal.data["opening_chat_message"] == "o"
    assert proposal.notification_type == rx.NOTIFICATION_TYPE_REENGAGE


async def test_on_reengage_delivered_noop_on_no_delivery():
    # Must not raise (and nothing to record) when the drain couldn't deliver.
    await rx.on_reengage_delivered(
        SimpleNamespace(user_id="u1"),
        NotificationResult(tokens_targeted=1, success_count=0, failure_count=1),
    )
