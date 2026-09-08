"""Deterministic age banding from the account's own declared date of birth.

WHY THIS EXISTS
``date_of_birth`` was collected at onboarding (handlers/account_onboarding.py)
and then never read again by anything. A grep across src/ returned only the
onboarding handler itself. Two consequences shipped:

1. A 13-to-17-year-old was byte-identical to an adult at inference time. The
   band was only observable indirectly, as ``aura_consent_granted == False``,
   which is indistinguishable from an adult who simply declined memory. So the
   under-18 policy the onboarding UI already PROMISES the user ("Aura memory
   (unavailable under 18)", "Behavioral profiling is disabled for users under
   18") could not be honoured anywhere downstream, because nothing downstream
   could ask the question.
2. The band was decided once, at signup, forever. A user who onboarded at 17
   stayed 17 to the system. Resolving per turn from the stored DOB fixes that
   with no backfill and no migration: they become ADULT on their birthday.

WHAT THIS IS NOT
This is not age *inference*. It reads the declared date of birth and nothing
else: no linguistic profiling, no behavioural signals, no classifier. That is a
deliberate limit, not an unfinished one. Behavioural age profiling misclassifies
at scale (a 95%-accurate classifier is still enormously wrong across a real user
base, and adults get flagged for ordinary usage patterns), and every false
positive lands on a real person as an unexplained restriction. A self-attested
DOB is weak evidence, but it is the user's own claim and it is stable, auditable
and explainable. Contradicting evidence is handled by age_signals.py, which
escalates to a human and never silently reclassifies anyone.

FAILURE DIRECTION
Fails to UNKNOWN, never to ADULT. A missing, malformed, or nonsense DOB must not
resolve to "adult, no restrictions"; that would make data corruption the most
permissive state. UNKNOWN deliberately carries no teen restrictions either (see
``applies_minor_policy``): every legacy account predating the DOB field would
otherwise be restricted for no safety benefit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

# The floor enforced at signup (handlers/account_onboarding.py:_MINIMUM_AGE).
# Duplicated as a comparison boundary only; onboarding remains the one writer.
MINIMUM_AGE = 13

# The age at which Aura treats the account as an adult for consent and for
# conversational policy. Matches the coercion already in onboarding
# (``effective_consent = consent if age >= 18 else False``) and the copy shown
# to the user on the consent screen.
ADULT_AGE = 18

# Rejects typos and placeholder dates without needing a full calendar check.
_EARLIEST_PLAUSIBLE_BIRTH = date(1900, 1, 1)


class AgeBand(str, Enum):
    """The four states an account's declared age can be in.

    ``str`` mixin so a band can be logged or stored without a conversion.
    """

    UNKNOWN = "unknown"
    UNDER_13 = "under_13"
    TEEN_13_17 = "teen_13_17"
    ADULT = "adult"


@dataclass(frozen=True)
class AgeBandDecision:
    """A resolved band plus the evidence it came from.

    ``declared_age`` is None whenever the band is UNKNOWN, so a caller can never
    read an age that was not actually derived from a usable date.
    """

    band: AgeBand
    declared_age: int | None
    date_of_birth: str


def age_on(born: date, today: date) -> int:
    """Whole years elapsed, birthday-exact.

    Mirrors ``_age_on`` in handlers/account_onboarding.py deliberately: the
    signup gate and every later read must agree to the day, or a user could be
    admitted at one age and banded at another on the same afternoon.
    """
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def resolve_age_band(
    user_doc: dict[str, Any] | None, now: datetime | None = None
) -> AgeBandDecision:
    """Band an already-fetched ``users/{uid}`` document. Pure; no I/O.

    Takes the doc rather than a uid, matching ``resolve_effective_tier`` in
    services/entitlement.py, so the many callers that already hold the user doc
    for this turn pay nothing extra to ask the question.
    """
    current = (now or datetime.now(UTC)).date()
    raw = (user_doc or {}).get("date_of_birth")

    if not isinstance(raw, str) or len(raw) != 10:
        return AgeBandDecision(band=AgeBand.UNKNOWN, declared_age=None, date_of_birth="")
    try:
        born = date.fromisoformat(raw)
    except ValueError:
        return AgeBandDecision(band=AgeBand.UNKNOWN, declared_age=None, date_of_birth="")

    # A future or pre-1900 date is corrupt input, not a very old or unborn user.
    # Treating it as an age would band somebody on a number nobody entered.
    if born < _EARLIEST_PLAUSIBLE_BIRTH or born > current:
        return AgeBandDecision(band=AgeBand.UNKNOWN, declared_age=None, date_of_birth=raw)

    age = age_on(born, current)
    if age < MINIMUM_AGE:
        band = AgeBand.UNDER_13
    elif age < ADULT_AGE:
        band = AgeBand.TEEN_13_17
    else:
        band = AgeBand.ADULT
    return AgeBandDecision(band=band, declared_age=age, date_of_birth=raw)


def band_from_value(value: Any) -> AgeBand:
    """Coerce a stored/serialized band back to the enum, tolerantly.

    Anything unrecognized becomes UNKNOWN rather than raising. A band that has
    travelled through a dict projection or a cache is untrusted input by the
    time it comes back, and a ValueError here would take down a live voice
    session over a typo in a field nobody reads directly.
    """
    if isinstance(value, AgeBand):
        return value
    try:
        return AgeBand(str(value))
    except ValueError:
        return AgeBand.UNKNOWN


def applies_minor_policy(band: AgeBand) -> bool:
    """Whether the minor conversation policy is owed for this band.

    UNDER_13 is included even though onboarding refuses that age: the value can
    still arrive through a legacy document or a client that skipped the gate,
    and the more protective reading is the correct one for a band we should
    never have admitted.

    UNKNOWN is excluded on purpose. See the module docstring: restricting every
    account with no usable DOB buys no safety and costs real users.
    """
    return band in (AgeBand.UNDER_13, AgeBand.TEEN_13_17)
