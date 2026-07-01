"""
Buddy Keyboard vocab hints: a small, consent-gated set of the user's own proper-noun-ish
words (interest subjects + storyline entities) that the on-device keyboard caches and treats
as KNOWN words, so a friend's or interest's name is never flagged as a misspelling or
autocorrected, and is boosted in the suggestion strip.

Memory CONSUMER, never a producer. It only READS the UserAura profile through the schema
accessors (no raw field reads), via ``fetch_cached_aura_data`` which already applies the
consent gate (empty when memory is revoked) and a server-side cache. Read-only; nothing the
user types is involved here at all.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..chat_completion.prompt_builder import fetch_cached_aura_data
from ..user_aura_schema import ranked_storylines, top_interest_subjects

# Caps. The keyboard only needs a compact hint set; these bound the payload and the on-device
# cache. Tune once real usage exists.
VOCAB_TOKENS_MAX = 80
SUBJECTS_SCAN = 30
STORYLINES_SCAN = 8
MIN_TOKEN_LENGTH = 2

# A token is a word (letters, with internal apostrophes/hyphens allowed). We split multi-word
# subjects like "XUV 3XO" into individual word tokens and drop anything with digits, since the
# keyboard matches single-word letter prefixes.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


class VocabHints(BaseModel):
    """The response shape: a flat list of known-word tokens for the keyboard."""

    tokens: list[str] = Field(default_factory=list)


def _tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    return [w for w in _TOKEN_RE.findall(text) if len(w) >= MIN_TOKEN_LENGTH]


async def build_vocab_hints(uid: str) -> VocabHints:
    """The user's known-word hint set, or empty when consent is absent / there is no profile.

    Never raises: a read failure or a missing profile each return an empty list, so the keyboard
    simply has no extra known words rather than an error.
    """
    try:
        profile, _ = await fetch_cached_aura_data(uid)
    except Exception as exc:
        logger.warn("keyboard.vocab: aura read failed", {"user_id": uid, "error": str(exc)})
        return VocabHints(tokens=[])

    # fetch_cached_aura_data returns {} when consent is revoked or there is no profile yet. That
    # is a legitimate empty result (no hints), distinct from the error path logged above.
    if not profile:
        return VocabHints(tokens=[])

    seen: set[str] = set()
    tokens: list[str] = []

    def add(text: str | None) -> None:
        for word in _tokenize(text):
            key = word.lower()
            if key not in seen:
                seen.add(key)
                tokens.append(word)

    for subject in top_interest_subjects(profile, k=SUBJECTS_SCAN):
        add(subject)
    for node in ranked_storylines(profile, k=STORYLINES_SCAN):
        for entity in node.get("entities") or []:
            add(entity)

    result = tokens[:VOCAB_TOKENS_MAX]
    logger.info("keyboard.vocab: ok", {"user_id": uid, "count": len(result)})
    return VocabHints(tokens=result)
