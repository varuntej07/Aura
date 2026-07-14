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
def clear_active_users_cache():
    """fcm_token_registry.list_active_user_ids caches its result in a module-level
    dict (in-process TTL cache, see fcm_token_registry.py). Without this, a result
    populated by one test's fake Firestore leaks into the next test that calls the
    same function within the TTL window, since pytest runs the whole suite in one
    process. Clear before AND after so a test's own cache write never survives it."""
    from src.services import fcm_token_registry

    fcm_token_registry._active_users_cache.clear()
    yield
    fcm_token_registry._active_users_cache.clear()


@pytest.fixture(autouse=True)
def clear_account_created_cache():
    """notification_budget caches each user's resolved account-creation timestamp
    in a module-level dict for the process lifetime (see
    notification_budget._account_created_cache). Without this, one test's fake
    Firebase Auth response leaks into the next test that resolves the same
    user_id, since pytest runs the whole suite in one process."""
    from src.services import notification_budget

    notification_budget._account_created_cache.clear()
    yield
    notification_budget._account_created_cache.clear()


@pytest.fixture(autouse=True)
def reset_openai_chat_fallback_client():
    """openai_chat_fallback caches its AsyncOpenAI client in a module-level
    singleton (see openai_chat_fallback._client), same lazy-init pattern as
    ModelProvider._get_gemini_client. Reset so one test's monkeypatched/fake
    client never survives into the next test in this same process."""
    from src.services import openai_chat_fallback

    openai_chat_fallback._client = None
    yield
    openai_chat_fallback._client = None
