"""
Global salience — "how big is this story worldwide", independent of any one user.

The signal engine's normal path is PERSONAL: it sends content close to a user's
own interest vector. But a genuinely massive story ("OpenAI releases GPT-6", a
major disaster) should reach EVERY user even if it's outside their declared
interests. That needs a second, orthogonal axis: salience.

The free, no-extra-cost salience signal is CROSS-EDITION OVERLAP. The Google News
ingest pulls the same topic sections (WORLD/NATION/…) for several locale editions
(US / IN / GB). A story that surfaces in ALL of them at once is, by construction,
globally important. The ingest counts how many editions carried a headline
(``edition_count``) instead of discarding the overlap on de-dup, and this module
turns that count (plus lead-position and the WORLD section) into a 0..1 score
stored on each pool candidate.

Design note — PRECISION over RECALL: cross-edition matching is by normalised
title, so differently-worded editions of the same event won't always merge. That
makes this a HIGH-PRECISION, low-recall signal: when editions DO agree on a
headline it is almost certainly the same huge story, so a false "breaking" is
very unlikely. Missing some big stories is acceptable — breaking is a rare bonus
lane, not the main personalised path. Only an all-editions story can clear the
breaking bar (see scoring.BREAKING_SALIENCE_BAR), so single-edition or two-edition
items can never fire an everyone-gets-it push.

Pure functions only — no I/O, referentially transparent, unit-tested directly.
"""

from __future__ import annotations

# Base salience by how many locale editions carried the same headline. Only an
# all-editions (3) story reaches the breaking bar on its own; one/two editions
# stay well below it so they can only ever act as a mild personal-lane nudge.
_EDITION_BASE: dict[int, float] = {1: 0.25, 2: 0.60, 3: 0.92}

# Lead-position bonus — Google News already ranks the section, so the lead item
# is the edition's most important story.
_LEAD_RANK_BONUS = 0.08
_NEAR_LEAD_RANK_BONUS = 0.04
_NEAR_LEAD_MAX_RANK = 2

# Small bump for the WORLD/global section (vs a niche topic feed).
_WORLD_SECTION_BONUS = 0.04


def compute_salience(
    *,
    edition_count: int,
    feed_rank: int = 0,
    is_world_section: bool = False,
) -> float:
    """Blend cross-edition overlap (dominant), lead position, and world-section
    into a 0..1 salience. Clamped to [0, 1].

    edition_count: distinct locale editions that carried this headline (>=1).
    feed_rank: 0-based position within its source feed (0 = lead story).
    is_world_section: True when it came from the WORLD/global news section.
    """
    editions = max(1, min(3, int(edition_count)))
    score = _EDITION_BASE[editions]

    if feed_rank <= 0:
        score += _LEAD_RANK_BONUS
    elif feed_rank <= _NEAR_LEAD_MAX_RANK:
        score += _NEAR_LEAD_RANK_BONUS

    if is_world_section:
        score += _WORLD_SECTION_BONUS

    return max(0.0, min(1.0, score))
