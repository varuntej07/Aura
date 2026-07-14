"""
Tests for src/handlers/buddy_pills.py — the on-demand Buddy chat pill refresh.

Focus: the recency window on the query fetch. Stale queries (an event already
finished, e.g. a meeting prepped for last week) must be filtered out before they
seed a suggestion pill, so the fetch applies a `timestamp >= cutoff` filter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch


class TestFetchRecentQueries:
    def test_applies_recency_window_filter(self):
        from src.handlers.buddy_pills import (
            _QUERY_RECENCY_WINDOW_DAYS,
            _fetch_recent_queries,
        )

        db = MagicMock()
        queries_col = (
            db.collection.return_value.document.return_value.collection.return_value
        )
        # Chain ...where().order_by().limit().stream() returns no docs.
        (
            queries_col.where.return_value
            .order_by.return_value
            .limit.return_value
            .stream.return_value
        ) = []

        before = datetime.now(UTC)
        with patch(
            "src.handlers.buddy_pills.admin_firestore", return_value=db
        ):
            result = _fetch_recent_queries("uid1")
        after = datetime.now(UTC)

        assert result == []
        # The queries subcollection is filtered, ordered, and capped at 10.
        db.collection.return_value.document.assert_called_once_with("uid1")
        queries_col.where.return_value.order_by.return_value.limit.assert_called_once_with(10)

        field, op, cutoff_iso = queries_col.where.call_args[0]
        assert field == "timestamp"
        assert op == ">="
        # Cutoff is ~_QUERY_RECENCY_WINDOW_DAYS days in the past (drops older queries).
        cutoff = datetime.fromisoformat(cutoff_iso)
        expected_lo = before - timedelta(days=_QUERY_RECENCY_WINDOW_DAYS)
        expected_hi = after - timedelta(days=_QUERY_RECENCY_WINDOW_DAYS)
        assert expected_lo <= cutoff <= expected_hi

    def test_returns_empty_on_firestore_error(self):
        from src.handlers.buddy_pills import _fetch_recent_queries

        with patch(
            "src.handlers.buddy_pills.admin_firestore",
            side_effect=RuntimeError("boom"),
        ):
            assert _fetch_recent_queries("uid1") == []
