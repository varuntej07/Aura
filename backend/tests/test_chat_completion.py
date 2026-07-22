"""Tests for durable background chat completion (services/chat_completion).

Covers the decision logic that finishes a turn the client disconnected from:
  * the SOURCE_CHAT_REPLY proposal contract (committed, untimed, ranked)
  * complete_turn: synthesize-from-tools (no LLM), regenerate, fail, skip, no-op
  * tool idempotency: the side-effecting set, key determinism, fail-open
The transactional claim paths mirror the already-tested reengagement/reminder claims.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from src.services.chat_completion import completion, tool_idempotency, turn_store
from src.services.notifications import proposal as proposal_mod


# ── Proposal contract ────────────────────────────────────────────────────────
def test_chat_reply_source_is_registered_everywhere():
    assert proposal_mod.SOURCE_CHAT_REPLY == "chat_reply"
    assert proposal_mod.SOURCE_CHAT_REPLY in proposal_mod.ALL_SOURCES
    # Ranked just under the time-exact reminder/tracking, above calendar.
    assert proposal_mod.PRIORITY[proposal_mod.SOURCE_CHAT_REPLY] == 94
    # The user's own awaited reply is untimed: deliver whenever it is ready.
    assert proposal_mod.FRESHNESS_MAX_AGE[proposal_mod.SOURCE_CHAT_REPLY] is None


def test_chat_reply_proposal_is_committed_and_never_stale():
    proposal = proposal_mod.NotificationProposal(
        user_id="u1",
        source=proposal_mod.SOURCE_CHAT_REPLY,
        kind=proposal_mod.ProposalKind.COMMITTED,
        dedup_key="chat_reply:c1",
    )
    assert proposal.kind == proposal_mod.ProposalKind.COMMITTED
    # notification_type defaults to the source, which is the client routing key.
    assert proposal.notification_type == "chat_reply"
    assert proposal_mod.is_stale(proposal, datetime.now(UTC)) is False


# ── complete_turn ────────────────────────────────────────────────────────────
async def test_complete_turn_synthesizes_when_a_tool_already_ran(monkeypatch):
    """A turn that already created a reminder must NOT re-run the LLM; it confirms, and
    hydrates the reminder card from the actual receipt (not text-only)."""
    claimed = {
        turn_store.FIELD_SESSION_ID: "s1",
        turn_store.FIELD_COMPLETED_TOOLS: ["set_reminder"],
        turn_store.FIELD_MESSAGE: "remind me at 5",
    }
    monkeypatch.setattr(turn_store, "claim_for_completion", AsyncMock(return_value=claimed))
    mark_complete = AsyncMock()
    monkeypatch.setattr(turn_store, "mark_complete", mark_complete)
    receipt = {"reminder_id": "r1", "title": "Call mom", "trigger_at": "2026-07-14T17:00:00Z"}
    monkeypatch.setattr(
        tool_idempotency, "get_turn_receipts",
        AsyncMock(return_value={"set_reminder": receipt}),
    )
    monkeypatch.setattr(
        completion, "_regenerate",
        AsyncMock(side_effect=AssertionError("must not regenerate a tool turn")),
    )
    pushed: dict = {}

    async def _fake_push(uid, cmid, sid, answer):
        pushed.update(uid=uid, cmid=cmid, sid=sid, answer=answer)

    monkeypatch.setattr(completion, "_push_reply", _fake_push)

    status = await completion.complete_turn("u1", "c1", "s1")

    assert status == "synthesized"
    mark_complete.assert_awaited_once()
    assert "reminder" in pushed["answer"].lower()
    # The receipt rides through to the turn doc so the client renders the card.
    assert mark_complete.await_args.kwargs["reminder"] == receipt


async def test_complete_turn_requires_receipt_for_non_reminder_side_effect(monkeypatch):
    """Every synthesized side-effect claim is grounded in the owning turn receipt."""
    claimed = {
        turn_store.FIELD_SESSION_ID: "s1",
        turn_store.FIELD_COMPLETED_TOOLS: ["track_topic"],
        turn_store.FIELD_MESSAGE: "keep me posted on the match",
    }
    monkeypatch.setattr(turn_store, "claim_for_completion", AsyncMock(return_value=claimed))
    monkeypatch.setattr(turn_store, "mark_complete", AsyncMock())
    receipts = AsyncMock(return_value={"track_topic": {"topic_key": "match"}})
    monkeypatch.setattr(tool_idempotency, "get_turn_receipts", receipts)
    monkeypatch.setattr(completion, "_push_reply", AsyncMock())

    status = await completion.complete_turn("u1", "c1", "s1")

    assert status == "synthesized"
    receipts.assert_awaited_once_with("u1", "c1")


async def test_complete_turn_noop_when_not_claimable(monkeypatch):
    """A terminal/already-claimed/missing turn never sends a duplicate push."""
    monkeypatch.setattr(turn_store, "claim_for_completion", AsyncMock(return_value=None))
    push = AsyncMock()
    monkeypatch.setattr(completion, "_push_reply", push)

    status = await completion.complete_turn("u1", "c1")

    assert status == "noop"
    push.assert_not_awaited()


async def test_complete_turn_regenerates_and_pushes(monkeypatch):
    claimed = {
        turn_store.FIELD_SESSION_ID: "s1",
        turn_store.FIELD_COMPLETED_TOOLS: [],
        turn_store.FIELD_MESSAGE: "capital of France?",
        turn_store.FIELD_HISTORY: [],
        turn_store.FIELD_TIER: "pro",
    }
    monkeypatch.setattr(turn_store, "claim_for_completion", AsyncMock(return_value=claimed))
    mark_complete = AsyncMock()
    monkeypatch.setattr(turn_store, "mark_complete", mark_complete)
    monkeypatch.setattr(completion, "_regenerate", AsyncMock(return_value=("Paris.", None, [])))
    push = AsyncMock()
    monkeypatch.setattr(completion, "_push_reply", push)

    status = await completion.complete_turn("u1", "c1")

    assert status == "regenerated"
    mark_complete.assert_awaited_once()
    push.assert_awaited_once()


async def test_complete_turn_marks_failed_and_skips_push_when_regen_empty(monkeypatch):
    claimed = {
        turn_store.FIELD_SESSION_ID: "s1",
        turn_store.FIELD_COMPLETED_TOOLS: [],
        turn_store.FIELD_MESSAGE: "hi",
        turn_store.FIELD_HISTORY: [],
        turn_store.FIELD_TIER: "pro",
    }
    monkeypatch.setattr(turn_store, "claim_for_completion", AsyncMock(return_value=claimed))
    mark_failed = AsyncMock()
    monkeypatch.setattr(turn_store, "mark_failed", mark_failed)
    monkeypatch.setattr(completion, "_regenerate", AsyncMock(return_value=("", None, [])))
    push = AsyncMock()
    monkeypatch.setattr(completion, "_push_reply", push)

    status = await completion.complete_turn("u1", "c1")

    assert status == "failed_empty"
    mark_failed.assert_awaited_once()
    push.assert_not_awaited()


async def test_complete_turn_skips_attachment_turn(monkeypatch):
    """Attachment turns are text-only in the doc, so a regen would answer a different
    question. They fail rather than mislead."""
    claimed = {
        turn_store.FIELD_SESSION_ID: "s1",
        turn_store.FIELD_COMPLETED_TOOLS: [],
        turn_store.FIELD_HAS_ATTACHMENTS: True,
    }
    monkeypatch.setattr(turn_store, "claim_for_completion", AsyncMock(return_value=claimed))
    mark_failed = AsyncMock()
    monkeypatch.setattr(turn_store, "mark_failed", mark_failed)
    monkeypatch.setattr(
        completion, "_regenerate",
        AsyncMock(side_effect=AssertionError("must not regenerate an attachment turn")),
    )

    status = await completion.complete_turn("u1", "c1")

    assert status == "skipped_attachments"
    mark_failed.assert_awaited_once()


def test_synthesize_confirmation_prefers_known_tool_then_falls_back():
    assert "calendar" in completion._synthesize_confirmation(["create_calendar_event"]).lower()
    # An unknown tool name falls back to the generic warm confirmation.
    assert completion._synthesize_confirmation(["mystery_tool"]) == "Done, I took care of that for you."


def test_synthesize_confirmation_covers_every_committed_tool():
    """A multi-tool turn confirms all of them, not just the first (deduped, in order)."""
    out = completion._synthesize_confirmation(["set_reminder", "track_topic"])
    assert "reminder" in out.lower()
    assert "keep an eye" in out.lower()  # track_topic's confirmation
    # A tool repeated in the turn is confirmed exactly once.
    assert completion._synthesize_confirmation(["set_reminder", "set_reminder"]) == (
        completion._synthesize_confirmation(["set_reminder"])
    )


# ── Tool idempotency ─────────────────────────────────────────────────────────
def test_side_effecting_set_matches_the_state_changing_tools():
    assert tool_idempotency.SIDE_EFFECTING_TOOLS == frozenset({
        "set_reminder",
        "cancel_reminder",
        "create_calendar_event",
        "send_email",
        "store_memory",
        "track_topic",
        "cancel_tracker",
        "report_feedback",
    })
    # Read-only tools are NOT guarded (re-running them on a regen is harmless).
    assert "web_surf" not in tool_idempotency.SIDE_EFFECTING_TOOLS
    assert "list_reminders" not in tool_idempotency.SIDE_EFFECTING_TOOLS


def test_idempotency_key_is_deterministic_and_args_sensitive():
    a = tool_idempotency._key("c1", "set_reminder", {"x": 1, "y": 2})
    b = tool_idempotency._key("c1", "set_reminder", {"y": 2, "x": 1})  # order independent
    c = tool_idempotency._key("c1", "set_reminder", {"x": 1, "y": 3})  # different args
    d = tool_idempotency._key("c2", "set_reminder", {"x": 1, "y": 2})  # different turn
    assert a == b
    assert a != c
    assert a != d
    assert a.startswith("c1:set_reminder:")


async def test_idempotency_fails_open_when_store_unreachable(monkeypatch):
    """If the idempotency store can't be reached, the tool still runs (a rare duplicate
    beats silently dropping a user-requested action)."""
    def _boom():
        raise RuntimeError("firestore down")

    monkeypatch.setattr(tool_idempotency, "admin_firestore", _boom)
    ran: dict = {}

    async def _handler(inp):
        ran["called"] = True
        return {"ok": True, "echo": inp}

    result = await tool_idempotency.run_idempotent(
        "u1", "c1", "set_reminder", {"x": 1}, _handler
    )

    assert ran.get("called") is True
    assert result == {"ok": True, "echo": {"x": 1}}


# ── Firestore index contract (deploy-order guard) ────────────────────────────
def test_chat_turns_collection_group_index_declared():
    import os

    indexes_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "firestore.indexes.json",
    )
    with open(indexes_path, encoding="utf-8") as fh:
        indexes = json.load(fh)["indexes"]

    match = [
        idx for idx in indexes
        if idx.get("collectionGroup") == "chat_turns"
        and idx.get("queryScope") == "COLLECTION_GROUP"
    ]
    assert len(match) == 1, "chat_turns COLLECTION_GROUP index missing (backstop sweep 400s without it)"
    field_paths = [f["fieldPath"] for f in match[0]["fields"]]
    assert field_paths == ["status", "created_at"]
