"""Orchestrator: the pure decision logic + the two lanes.

Pure helpers (freshness, arbitration, dedup) run with no I/O. The lane tests
monkeypatch the orchestrator's I/O seams (queue store, ledger dedup read, budget,
the FCM deliver, and the local-time fetch) so the routing, drop reasons, quiet
hold, and priority arbitration are exercised end-to-end without Firestore or FCM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.services import notification_budget, notification_ledger
from src.services.notification_budget import BudgetDecision
from src.services.notification_service import NotificationResult
from src.services.notifications import orchestrator, post_send, proposal, queue_store, tap_gate
from src.services.reactive import idempotency
from src.services.notifications.proposal import (
    Disposition,
    NotificationProposal,
    ProposalKind,
    SOURCE_ICEBREAKER,
    SOURCE_NEWS,
    SOURCE_REMINDER,
    SOURCE_THREAD,
    SOURCE_TRACKING,
)

NOW = datetime(2026, 6, 21, 15, 0, tzinfo=UTC)  # 15:00 UTC, a non-quiet hour in UTC


def _proposal(source: str, *, kind: ProposalKind, dedup_key: str = "k",
              content_ts: datetime | None = None,
              max_age: timedelta | None = None,
              priority: int | None = None) -> NotificationProposal:
    return NotificationProposal(
        user_id="u1",
        source=source,
        kind=kind,
        dedup_key=dedup_key,
        title="t",
        body="b",
        content_timestamp=content_ts,
        freshness_max_age=max_age,
        priority=priority,
    )


# ── Pure: priority + freshness defaults/overrides ────────────────────────────
def test_effective_priority_uses_table_then_override():
    p = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE)
    assert p.effective_priority == proposal.PRIORITY[SOURCE_NEWS]  # 10
    p2 = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, priority=99)
    assert p2.effective_priority == 99


def test_reminder_edges_out_tracking():
    assert proposal.PRIORITY[SOURCE_REMINDER] > proposal.PRIORITY[SOURCE_TRACKING]


def test_is_stale_respects_window():
    # news window is 18h; a 20h-old item is stale, a 2h-old item is fresh.
    old = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE,
                    content_ts=NOW - timedelta(hours=20))
    fresh = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE,
                      content_ts=NOW - timedelta(hours=2))
    assert proposal.is_stale(old, NOW) is True
    assert proposal.is_stale(fresh, NOW) is False


def test_is_stale_false_without_window_or_timestamp():
    # thread has no window
    t = _proposal(SOURCE_THREAD, kind=ProposalKind.PROACTIVE,
                  content_ts=NOW - timedelta(days=5))
    assert proposal.is_stale(t, NOW) is False
    # news with a window but no timestamp can't be judged stale
    n = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, content_ts=None)
    assert proposal.is_stale(n, NOW) is False


def test_per_proposal_max_age_override():
    # tracking default is 6h, but a live-match proposal tightens it to 1h
    p = _proposal(SOURCE_TRACKING, kind=ProposalKind.COMMITTED,
                  content_ts=NOW - timedelta(hours=2), max_age=timedelta(hours=1))
    assert proposal.is_stale(p, NOW) is True


# ── Pure: arbitration ────────────────────────────────────────────────────────
def test_arbitrate_highest_priority_wins():
    thread = _proposal(SOURCE_THREAD, kind=ProposalKind.PROACTIVE, dedup_key="a")
    news = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b")
    ice = _proposal(SOURCE_ICEBREAKER, kind=ProposalKind.PROACTIVE, dedup_key="c")
    winner, losers = proposal.arbitrate([news, ice, thread])
    assert winner.source == SOURCE_THREAD       # 70 beats 20 and 10
    assert {p.source for p in losers} == {SOURCE_NEWS, SOURCE_ICEBREAKER}


def test_arbitrate_tiebreak_prefers_recent_timestamp():
    older = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="a",
                      content_ts=NOW - timedelta(hours=5))
    newer = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b",
                      content_ts=NOW - timedelta(hours=1))
    winner, _ = proposal.arbitrate([older, newer])
    assert winner.dedup_key == "b"


def test_arbitrate_empty():
    assert proposal.arbitrate([]) == (None, [])


# ── Lane: committed (express) ────────────────────────────────────────────────
async def test_committed_fresh_sends_and_records(monkeypatch):
    delivered: list[NotificationProposal] = []
    recorded: list[str] = []

    async def _fake_deliver(p):
        delivered.append(p)
        return NotificationResult(tokens_targeted=1, success_count=1, failure_count=0)

    async def _claim(key, *, scope, ttl=None):
        return True

    async def _record(uid, *, source, now=None):
        recorded.append(source)

    monkeypatch.setattr(orchestrator, "_deliver", _fake_deliver)
    monkeypatch.setattr(idempotency, "idempotent", _claim)
    monkeypatch.setattr(notification_budget, "record_committed_send", _record)

    p = _proposal(SOURCE_REMINDER, kind=ProposalKind.COMMITTED)
    decision = await orchestrator.submit(p, now=NOW)

    assert decision.disposition == Disposition.SEND
    assert len(delivered) == 1
    assert recorded == [SOURCE_REMINDER]


async def test_committed_stale_drops(monkeypatch):
    delivered: list = []

    async def _fake_deliver(p):
        delivered.append(p)
        return NotificationResult(tokens_targeted=1, success_count=1, failure_count=0)

    monkeypatch.setattr(orchestrator, "_deliver", _fake_deliver)

    p = _proposal(SOURCE_TRACKING, kind=ProposalKind.COMMITTED,
                  content_ts=NOW - timedelta(hours=10), max_age=timedelta(hours=3))
    decision = await orchestrator.submit(p, now=NOW)

    assert decision.disposition == Disposition.DROP
    assert decision.reason == proposal.REASON_STALE
    assert delivered == []


async def test_committed_duplicate_drops(monkeypatch):
    delivered: list = []

    async def _fake_deliver(p):
        delivered.append(p)
        return NotificationResult(tokens_targeted=1, success_count=1, failure_count=0)

    async def _claim(key, *, scope, ttl=None):
        return False  # already claimed elsewhere = duplicate

    monkeypatch.setattr(orchestrator, "_deliver", _fake_deliver)
    monkeypatch.setattr(idempotency, "idempotent", _claim)

    p = _proposal(SOURCE_TRACKING, kind=ProposalKind.COMMITTED, dedup_key="dupe")
    decision = await orchestrator.submit(p, now=NOW)

    assert decision.disposition == Disposition.DROP
    assert decision.reason == proposal.REASON_DUPLICATE
    assert delivered == []


# ── Lane: proactive (queued) ─────────────────────────────────────────────────
async def test_proactive_submit_enqueues(monkeypatch):
    enqueued: list[NotificationProposal] = []

    async def _enqueue(p, *, now=None):
        enqueued.append(p)
        return "pid"

    monkeypatch.setattr(queue_store, "enqueue", _enqueue)

    p = _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE)
    decision = await orchestrator.submit(p, now=NOW)

    assert decision.disposition == Disposition.HOLD
    assert decision.reason == "queued"
    assert len(enqueued) == 1


def _patch_drain_io(monkeypatch, *, pending, quiet=False, budget_ok=True, active_tracker=False):
    """Wire the drain's I/O seams. ``pending`` is a list of (pid, proposal)."""
    marks: list[tuple[str, str]] = []
    delivered: list[NotificationProposal] = []

    async def _list_pending(uid, *, limit=50):
        return list(pending)

    async def _no_recent(uid, *, since):
        return set()

    async def _claim_dedup(key, *, scope, ttl=None):
        return True

    async def _release_dedup(key, *, scope):
        pass

    async def _user_local(uid, now):
        # 03:00 local = quiet; 15:00 local = awake
        local = now.replace(hour=3) if quiet else now.replace(hour=15)
        return local, local.date().isoformat()

    async def _claim_budget(uid, *, source, user_local_date=None, now=None, priority=False):
        return BudgetDecision(budget_ok, None if budget_ok else "global_daily_cap")

    async def _mark(uid, pid, status, *, now=None):
        marks.append((pid, status))

    async def _deliver(p):
        delivered.append(p)
        return NotificationResult(tokens_targeted=1, success_count=1, failure_count=0)

    async def _tap_passes(p):
        return True, "ok"

    async def _dispatch(p, r):
        return None

    async def _preferred_slot(uid, local_now):
        return True

    async def _active_tracker(uid, now):
        return active_tracker

    monkeypatch.setattr(queue_store, "list_pending", _list_pending)
    monkeypatch.setattr(queue_store, "mark", _mark)
    monkeypatch.setattr(notification_ledger, "recent_dedup_keys", _no_recent)
    monkeypatch.setattr(idempotency, "idempotent", _claim_dedup)
    monkeypatch.setattr(idempotency, "release", _release_dedup)
    monkeypatch.setattr(notification_budget, "try_claim_proactive_slot", _claim_budget)
    monkeypatch.setattr(orchestrator, "_user_local", _user_local)
    monkeypatch.setattr(orchestrator, "_deliver", _deliver)
    monkeypatch.setattr(orchestrator, "_has_active_tracker", _active_tracker)
    # The tap-gate (LLM), post-send dispatch, and smart-timing slot read are I/O seams
    # too — patch them so the drain tests exercise routing without an LLM call, producer
    # bookkeeping, or Firestore. Dedicated tests below cover the real tap-gate drop and
    # the off-peak smart-timing hold.
    monkeypatch.setattr(tap_gate, "passes", _tap_passes)
    monkeypatch.setattr(post_send, "dispatch_post_send", _dispatch)
    monkeypatch.setattr(orchestrator, "_is_preferred_slot", _preferred_slot)
    return marks, delivered


async def test_drain_arbitrates_highest_priority(monkeypatch):
    thread = ("p_thread", _proposal(SOURCE_THREAD, kind=ProposalKind.PROACTIVE, dedup_key="a"))
    news = ("p_news", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[news, thread])

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision.disposition == Disposition.SEND
    assert len(delivered) == 1 and delivered[0].source == SOURCE_THREAD
    assert ("p_thread", queue_store.STATUS_SENT) in marks
    assert ("p_news", queue_store.STATUS_HELD) in marks


async def test_drain_quiet_hours_holds_all(monkeypatch):
    news = ("p_news", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[news], quiet=True)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision.disposition == Disposition.HOLD
    assert decision.reason == proposal.REASON_QUIET_HOURS
    assert delivered == []
    assert ("p_news", queue_store.STATUS_HELD) in marks


async def test_drain_active_tracker_holds_all(monkeypatch):
    # A tracker update fired recently (a live match in progress) — unrelated proactive
    # content (thread/icebreaker/news) is held so it doesn't dilute the tracked event,
    # per the user's report of a flood of unrelated pushes during a live World Cup match.
    news = ("p_news", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[news], active_tracker=True)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision.disposition == Disposition.HOLD
    assert decision.reason == proposal.REASON_ACTIVE_TRACKER
    assert delivered == []
    assert ("p_news", queue_store.STATUS_HELD) in marks


async def test_drain_drops_stale_and_duplicate(monkeypatch):
    stale = ("p_stale", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="s",
                                  content_ts=NOW - timedelta(hours=30)))
    fresh = ("p_fresh", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="f",
                                  content_ts=NOW - timedelta(hours=1)))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[stale, fresh])

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert ("p_stale", queue_store.STATUS_DROPPED) in marks
    assert decision.disposition == Disposition.SEND
    assert delivered[0].dedup_key == "f"


async def test_drain_budget_denied_holds_all(monkeypatch):
    news = ("p_news", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[news], budget_ok=False)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision.disposition == Disposition.HOLD
    assert decision.reason == proposal.REASON_BUDGET
    assert delivered == []
    assert ("p_news", queue_store.STATUS_HELD) in marks


async def test_drain_empty_queue_returns_none(monkeypatch):
    marks, delivered = _patch_drain_io(monkeypatch, pending=[])
    decision = await orchestrator.drain_user_queue("u1", now=NOW)
    assert decision is None
    assert delivered == []


async def test_drain_drops_winner_failing_tap_gate(monkeypatch):
    # A winner the tap-gate judges low-value is DROPPED (silence beats spam), never sent.
    news = ("p_news", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[news])

    async def _reject(p):
        return False, "generic filler"

    monkeypatch.setattr(tap_gate, "passes", _reject)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision is not None
    assert decision.disposition == Disposition.DROP
    assert decision.reason == proposal.REASON_TAP_GATE
    assert delivered == []
    assert ("p_news", queue_store.STATUS_DROPPED) in marks


async def test_drain_drops_winner_failing_dedup_claim(monkeypatch):
    # Two overlapping drains (or a drain racing a committed send) sharing a
    # dedup_key: the loser of the atomic claim drops the winner as a duplicate
    # instead of sending, and still holds the other queued losers.
    news = ("p_news", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b"))
    other = ("p_other", _proposal(SOURCE_THREAD, kind=ProposalKind.PROACTIVE, dedup_key="c"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[news, other])

    async def _claim_taken(key, *, scope, ttl=None):
        return False

    monkeypatch.setattr(idempotency, "idempotent", _claim_taken)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision is not None
    assert decision.disposition == Disposition.DROP
    assert decision.reason == proposal.REASON_DUPLICATE
    assert delivered == []
    assert ("p_other", queue_store.STATUS_DROPPED) in marks  # arbitration winner (thread > news)
    assert ("p_news", queue_store.STATUS_HELD) in marks       # loser held, not lost


async def test_drain_holds_news_off_peak(monkeypatch):
    # A news winner (priority 10) in a weak engagement slot is HELD for a better hour.
    news = ("p_news", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="b"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[news])

    async def _off_peak(uid, local_now):
        return False

    monkeypatch.setattr(orchestrator, "_is_preferred_slot", _off_peak)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision is not None
    assert decision.disposition == Disposition.HOLD
    assert decision.reason == proposal.REASON_OFF_PEAK
    assert delivered == []
    assert ("p_news", queue_store.STATUS_HELD) in marks


async def test_drain_breaking_news_bypasses_smart_timing(monkeypatch):
    # A breaking item (lane=breaking) must NOT be held by smart timing even off-peak.
    breaking = ("p_brk", _proposal(SOURCE_NEWS, kind=ProposalKind.PROACTIVE, dedup_key="x"))
    breaking[1].data = {"lane": "breaking"}
    marks, delivered = _patch_drain_io(monkeypatch, pending=[breaking])

    async def _off_peak(uid, local_now):
        return False

    monkeypatch.setattr(orchestrator, "_is_preferred_slot", _off_peak)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision is not None and decision.disposition == Disposition.SEND
    assert delivered and delivered[0].dedup_key == "x"


async def test_drain_runs_post_send_only_on_delivery(monkeypatch):
    # The winner's producer-specific bookkeeping runs via dispatch_post_send, after a
    # real delivery, with the delivery result.
    thread = ("p_thread", _proposal(SOURCE_THREAD, kind=ProposalKind.PROACTIVE, dedup_key="a"))
    marks, delivered = _patch_drain_io(monkeypatch, pending=[thread])
    dispatched: list[tuple[str, bool]] = []

    async def _capture(p, r):
        dispatched.append((p.source, r.delivered))

    monkeypatch.setattr(post_send, "dispatch_post_send", _capture)

    decision = await orchestrator.drain_user_queue("u1", now=NOW)

    assert decision is not None and decision.disposition == Disposition.SEND
    assert dispatched == [(SOURCE_THREAD, True)]
