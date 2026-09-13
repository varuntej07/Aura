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


# Cut the test suite off from live Langfuse BEFORE any src module imports the
# telemetry client. A real backend/.env carries working LANGFUSE keys, and only
# test_llm_telemetry.py blanked them, so every other test that exercised an LLM
# path shipped its mocks to the production project: 29,879 observations there
# carry fixture uids ("u1", "uid-1") and raw MagicMock reprs 17-19KB long, under
# models like "claude-bogus". That noise is indistinguishable from real spend in
# a cost dashboard, and each mock repr is a paid ingestion unit.
#
# Module level, not an autouse fixture, and it resets the memoised globals as well
# as the settings: llm_telemetry._client is built once and cached, so a client
# constructed before a fixture ran would survive any later settings edit.
from src.config.settings import settings as _settings  # noqa: E402
from src.services.analytics import llm_telemetry as _llm_telemetry  # noqa: E402

_settings.LANGFUSE_PUBLIC_KEY = ""
_settings.LANGFUSE_SECRET_KEY = ""
_llm_telemetry._client = None
_llm_telemetry._init_attempted = False


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


@pytest.fixture(autouse=True)
def clear_get_better_catalog_cache():
    """Keep the process-level Get Better catalog cache isolated per test."""

    from src.services.get_better.catalog import clear_catalog_cache_for_testing

    clear_catalog_cache_for_testing()
    yield
    clear_catalog_cache_for_testing()
