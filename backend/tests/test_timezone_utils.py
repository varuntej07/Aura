from datetime import UTC, datetime

import pytest

from src.services.timezone_utils import (
    TimezoneResolutionError,
    canonicalize_timezone_name,
    localize,
)


def test_legacy_calcutta_alias_resolves_to_canonical_kolkata():
    assert canonicalize_timezone_name("Asia/Calcutta") == "Asia/Kolkata"
    localized = localize(datetime(2026, 7, 20, 19, 40, tzinfo=UTC), "Asia/Calcutta")
    assert localized.date().isoformat() == "2026-07-21"
    assert localized.utcoffset().total_seconds() == 5.5 * 60 * 60


@pytest.mark.parametrize("value", [None, "", "Not/A_Timezone"])
def test_missing_or_unknown_timezone_fails_explicitly(value):
    with pytest.raises(TimezoneResolutionError):
        canonicalize_timezone_name(value)
