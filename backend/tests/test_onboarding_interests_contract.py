"""
Cross-language contract for the onboarding interest picker.

The Flutter picker must offer exactly the backend's producible interest set. If the
two drift, a user can declare an interest the pool can never satisfy (silently no
content) or the backend can silently drop a declared slug. This test reads the Dart
constants file and asserts its slug set equals ONBOARDABLE_CATEGORIES, so a rename
on either side breaks CI (mirrors test_funnel_event_contract).
"""

from __future__ import annotations

import re
from pathlib import Path

from src.services.signal_engine.content_category_map import ONBOARDABLE_CATEGORIES

_DART_FILE = (
    Path(__file__).resolve().parents[2]
    / "lib"
    / "core"
    / "onboarding"
    / "onboardable_interests.dart"
)


def _dart_slugs() -> set[str]:
    text = _DART_FILE.read_text(encoding="utf-8")
    # Matches: OnboardableInterest('slug', 'Label')
    return set(re.findall(r"OnboardableInterest\(\s*'([^']+)'", text))


def test_dart_onboardable_file_exists():
    assert _DART_FILE.is_file(), f"Dart onboardable interests missing at {_DART_FILE}"


def test_dart_picker_slugs_equal_backend_producible_set():
    assert _dart_slugs() == set(ONBOARDABLE_CATEGORIES), (
        "Onboarding picker drift: the Dart slug set must equal "
        "ONBOARDABLE_CATEGORIES. Update lib/core/onboarding/onboardable_interests.dart "
        "or content_category_map.py so they match."
    )
