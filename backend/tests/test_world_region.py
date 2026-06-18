"""Tests for world_region — timezone → region resolution for the world briefing.

The contract that matters: a known zone maps to the right country, and an unknown /
missing / UTC zone degrades to the GLOBAL sentinel (global-only briefing) rather than
guessing a wrong country.
"""

from __future__ import annotations

from src.services.briefing.world_region import (
    GLOBAL_REGION_CODE,
    resolve_region,
)


def test_known_zones_map_to_country():
    assert resolve_region("Asia/Kolkata").country_code == "IN"
    assert resolve_region("Asia/Kolkata").country_name == "India"
    assert resolve_region("America/Los_Angeles").country_code == "US"
    assert resolve_region("Europe/London").country_code == "GB"
    assert resolve_region("Australia/Sydney").country_code == "AU"
    assert resolve_region("America/Sao_Paulo").country_code == "BR"


def test_legacy_calcutta_alias_maps_to_india():
    assert resolve_region("Asia/Calcutta").country_code == "IN"


def test_whitespace_is_tolerated():
    assert resolve_region("  Asia/Kolkata  ").country_code == "IN"


def test_unknown_zone_is_global():
    region = resolve_region("Mars/Olympus_Mons")
    assert region.country_code == GLOBAL_REGION_CODE
    assert region.is_global is True
    assert region.country_name == ""


def test_missing_or_utc_zone_is_global():
    assert resolve_region(None).is_global is True
    assert resolve_region("").is_global is True
    # "UTC" is not a country zone; it should not resolve to a real region.
    assert resolve_region("UTC").is_global is True
