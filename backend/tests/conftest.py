"""
Shared test fixtures.

All tests run with Firebase initialization mocked so no real GCP calls are made.
Individual test modules patch admin_firestore / admin_messaging at their usage site.
"""

from __future__ import annotations

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

# Ensure `src` is importable when pytest runs from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def mock_firebase_app():
    """Prevent real Firebase SDK initialization across every test."""
    mock_app = MagicMock()
    with patch.dict("firebase_admin._apps", {"[DEFAULT]": mock_app}, clear=False):
        with patch("firebase_admin.get_app", return_value=mock_app):
            with patch("firebase_admin.initialize_app", return_value=mock_app):
                yield mock_app


@pytest.fixture(autouse=True)
def disable_langsmith_tracing(monkeypatch):
    """Disable LangSmith tracing so tests don't require a real API key."""
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
