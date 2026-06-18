"""Manual stress test for the signal-engine notification framer.

Runs a spread of content fixtures (good, thin, off-topic, cold-start) through the
REAL framer LLM and prints the copy plus the hard-rule linter result, so you can
eyeball what users would actually receive and catch bad copy before they do.

Needs backend env (GEMINI_API_KEY etc). Run from the backend directory:

    cd backend && python scripts/stress_test_framer.py

Unlike tests/test_notification_framer_copy.py (pure, runs in CI), this calls the
model, so it is a dev tool, not a CI gate.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

# Make `src...` importable when run as a loose script (sys.path[0] would be scripts/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/

from src.services.model_provider import get_model_provider  # noqa: E402
from src.services.signal_engine.content_pool import ScoredCandidate  # noqa: E402
from src.services.signal_engine.notification_framer import (  # noqa: E402
    UserFramingContext,
    copy_violations,
    frame_notification,
)


def _cand(
    *,
    title: str,
    body: str,
    url: str = "https://example.com/article",
    category: str = "news",
    source: str = "newsdata",
) -> ScoredCandidate:
    return ScoredCandidate(
        content_id=f"{source}_{abs(hash(title)) % 10**8}",
        source=source,
        category=category,
        title=title,
        body=body,
        url=url,
        embedding=[0.0],
        freshness_ts=datetime.now(UTC),
        cosine_similarity=0.7,
    )


def _fixtures() -> list[tuple[str, ScoredCandidate, UserFramingContext, str]]:
    """(label, candidate, user_context, what we expect) tuples covering the cases
    that matter: a juicy send, two rejects, a specific-subject send, and the two
    cold-start outcomes (category match sends, off-topic rejects)."""
    f1_user = UserFramingContext(
        top_interests=["Formula 1", "Verstappen", "KCR"],
        dominant_tone="hyped", has_specific_interests=True, name="Varun",
    )
    ev_user = UserFramingContext(top_interests=["Tesla", "EVs"], has_specific_interests=True)
    coldstart = UserFramingContext(
        top_interests=["Sports", "Technology", "News"], has_specific_interests=False,
    )
    return [
        ("relevant F1 (should send, juicy, no source name)",
         _cand(title="Verstappen wins Monaco after a late safety car",
               body="Max Verstappen held off a charging field over the final three laps after a late safety-car restart bunched the pack at Monaco.",
               category="sports"),
         f1_user, "is_relevant=true"),
        ("off-topic for F1 user (should reject)",
         _cand(title="A new productivity app promises to fix your focus",
               body="A startup launched a focus timer with calendar integration and a freemium tier."),
         f1_user, "is_relevant=false"),
        ("thin / no-substance (should reject)",
         _cand(title="Every frame perfect", body=""),
         f1_user, "is_relevant=false (no body)"),
        ("Tesla for EV user (should send, specific subject)",
         _cand(title="Tesla quietly cuts Model Y price in two markets",
               body="Tesla lowered the Model Y starting price by a few percent in two markets, undercutting rivals.",
               category="business"),
         ev_user, "is_relevant=true"),
        ("cold-start Sports match (should send, category-level)",
         _cand(title="India chase 320 in the final over to take the series",
               body="India hunted down 320 with a boundary off the last over to clinch a tense decider.",
               category="sports"),
         coldstart, "is_relevant=true (cold-start category match)"),
        ("cold-start off-topic (should reject)",
         _cand(title="Local council debates parking permit rules",
               body="The city council met to discuss revised residential parking permit zones for next year."),
         coldstart, "is_relevant=false"),
    ]


async def main() -> None:
    models = get_model_provider()
    print("=" * 78)
    clean = 0
    flagged = 0
    for label, cand, ctx, expectation in _fixtures():
        framed = await frame_notification(models, cand, ctx)
        violations = copy_violations(framed)
        print(f"\n### {label}")
        print(f"    expected : {expectation}")
        print(f"    relevant : {framed.is_relevant}   content_kind: {framed.content_kind}")
        print(f"    title    : {framed.title!r}")
        print(f"    body     : {framed.body!r}")
        print(f"    opener   : {framed.opening_chat_message!r}")
        print(f"    reason   : {framed.relevance_reason!r}")
        if violations:
            flagged += 1
            print(f"    !! COPY VIOLATIONS: {violations}")
        else:
            clean += 1
            print("    copy     : clean")
    print("\n" + "=" * 78)
    print(f"clean={clean}  flagged={flagged}  total={clean + flagged}")


if __name__ == "__main__":
    asyncio.run(main())
