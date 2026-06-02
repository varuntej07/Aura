"""
Cross-language contract test for the re-engagement notification funnel.

The funnel only joins in PostHog if the server (Python) and client (Dart) use
byte-identical event-name and property-key strings. This test reads the Dart
constants file and asserts every Python funnel constant value is present there,
so a rename on either side breaks CI instead of silently flattening the funnel
(the "zero rows looks like healthy" failure this project has been bitten by).
"""

from __future__ import annotations

import re
from pathlib import Path

from src.services.analytics import funnel_events

# This file lives at backend/tests/; the repo root is two parents up, and the
# Dart mirror lives under lib/core/analytics/.
_DART_FILE = (
    Path(__file__).resolve().parents[2]
    / "lib"
    / "core"
    / "analytics"
    / "funnel_events.dart"
)


def _python_contract_values() -> set[str]:
    return {
        funnel_events.EVENT_NOTIFICATION_SENT,
        funnel_events.EVENT_NOTIFICATION_TAPPED,
        funnel_events.EVENT_SESSION_FROM_NOTIFICATION,
        funnel_events.EVENT_ACTION_AFTER_NOTIFICATION,
        funnel_events.PROP_NOTIFICATION_ID,
        funnel_events.PROP_CONTENT_ID,
        funnel_events.PROP_CATEGORY,
        funnel_events.PROP_NOTIFICATION_ORIGIN,
        funnel_events.PROP_FIREBASE_UID,
        funnel_events.NOTIFICATION_ORIGIN_SIGNAL_ENGINE,
    }


def _dart_string_literals() -> set[str]:
    text = _DART_FILE.read_text(encoding="utf-8")
    # Matches: static const String name = 'value';
    return set(re.findall(r"=\s*'([^']*)'\s*;", text))


def test_dart_funnel_constants_file_exists():
    assert _DART_FILE.is_file(), f"Dart funnel constants missing at {_DART_FILE}"


def test_every_server_funnel_value_exists_on_client():
    dart_values = _dart_string_literals()
    missing = _python_contract_values() - dart_values
    assert not missing, (
        f"Funnel contract drift: server values with no client match: {missing}. "
        "Update lib/core/analytics/funnel_events.dart to match "
        "backend/src/services/analytics/funnel_events.py."
    )
