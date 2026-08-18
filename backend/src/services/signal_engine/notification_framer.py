"""
One Gemini Flash call per (user, content) pair to produce notification copy.

The framer never decides whether to send. It only writes the title, body,
and opening_chat_message after scoring has picked the content and the user.

Input has two parts:
  - candidate: the chosen content_pool item (source title/body/url/category)
  - user_context: a small read-only summary derived from UserAura
                  (top interests, dominant tone) and current local time

Output is a Pydantic FramedNotification. If the LLM call fails or returns
malformed JSON, the framer falls back to a safe template that uses the raw
source title and a generic Buddy-voice opener. The scoring loop always
gets a valid result back; it never has to handle exceptions from here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ...prompts import (
    BREAKING_SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT,
    SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT,
    signal_notification_user_prompt,
)
from ..model_provider import ModelProvider
from .content_pool import ScoredCandidate

# Hard limits enforced after the model returns.
# The prompt says the same numbers but the LLM occasionally overshoots;
# truncation guarantees the FCM payload stays inside platform limits
NOTIFICATION_TITLE_MAX_CHARS = 50
NOTIFICATION_BODY_MAX_CHARS = 100
OPENING_CHAT_MESSAGE_MAX_CHARS = 280
# relevance_reason is a full sentence (audit trail, never shown in the push), so it
# gets a roomier cap than the title/body — enough for one explanatory sentence.
RELEVANCE_REASON_MAX_CHARS = 240

# content_kind drives the tap route on the client: "read" opens the source url in
# an in-app browser, "discuss" opens chat with Buddy. A candidate with no url can
# only ever be "discuss" (nothing to open), enforced after the model returns.
CONTENT_KIND_READ = "read"
CONTENT_KIND_DISCUSS = "discuss"

# Sentinel relevance_reason set by _safe_fallback when the framer LLM is unavailable
# (error or timeout). The scoring loop treats this as an INFRA outage — it defers
# the send for this tick and logs it loudly — NOT as a content-relevance rejection.
# Kept in one place so the producer (_safe_fallback) and the consumer (scoring_loop
# Gate B) can never drift; a sustained framer outage must never look like "nothing
# was relevant" (fail-loud doctrine).
FRAMER_UNAVAILABLE_REASON = "framer_unavailable"

# Stamped onto every framed notification's ledger row. Bump this whenever
# SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT changes so a tap-rate shift can be attributed to a
# specific copy revision (the A/B hook for "what phrasing gets the click").
FRAMER_PROMPT_VERSION = "2026-08-17"

# User-visible push copy must never contain long dashes (em "—" or en "–"); they read
# as machine-authored and the product voice forbids them. The framer prompt already
# tells the model to avoid them (BUDDY_VOICE_CORE), but the model occasionally slips,
# so this is the deterministic guarantee applied to every framed push before it leaves
# the service. Plain hyphens and double hyphens are intentionally left untouched; only
# the long dashes are rewritten, replaced (with any surrounding spaces) by a comma so
# the sentence still reads naturally.
_LONG_DASH_RUN = re.compile(r"\s*[—–]\s*")


def strip_long_dashes(text: str) -> str:
    """Replace em/en dashes (and the spaces around them) in user-visible copy with ', '."""
    if not text:
        return text
    cleaned = _LONG_DASH_RUN.sub(", ", text)
    # Trim a comma a leading/trailing dash may have left at the very start or end.
    return cleaned.strip().strip(",").strip()


def truncate_at_word_boundary(text: str, max_chars: int) -> str:
    """Shorten ``text`` to at most ``max_chars`` without ever splitting a word.

    A naive ``text[:max_chars]`` cut a real push body mid-token ("...for you" ->
    "...for y"), which reads as a glitch in the shade. This cuts at the last
    space before the limit instead, drops any trailing punctuation, and appends a
    single ellipsis so the trim reads as intentional. Text already within the cap
    is returned untouched; a single token longer than the cap (rare for push copy)
    falls back to a hard slice so the platform limit is always honoured."""
    if not text or len(text) <= max_chars:
        return text
    window = text[: max_chars - 1]  # leave room for the ellipsis
    cut = window.rfind(" ")
    truncated = (window[:cut] if cut > 0 else window).rstrip(" ,;:.")
    return f"{truncated}…"


class FramedNotification(BaseModel):
    title: str = Field(..., description="Push title, <= 50 chars.")
    body: str = Field(..., description="Push body, <= 100 chars.")
    opening_chat_message: str = Field(
        ...,
        description="One or two sentences Buddy opens with when the user taps."
    )
    # Gate B — the LLM relevance confirm. True only when the model can name the
    # specific interest this content matches for THIS user. Defaulted True at the
    # schema level so a model that omits the key does not crash; the scoring loop's
    # send gate is what enforces fail-CLOSED — it requires is_relevant AND a concrete
    # relevance_reason, so an affirmed-but-unexplained verdict still never sends.
    is_relevant: bool = Field(
        default=True,
        description="True ONLY if you can name the specific interest this matches.",
    )
    relevance_reason: str = Field(
        default="",
        description=(
            "The defensible reason this notification fires, written as ONE full "
            "plain-language sentence (not a couple of words): name the specific "
            "interest or subject it matches and why, or, when rejected, why it does "
            "not match. REQUIRED when is_relevant=true — an empty reason suppresses "
            "the send."
        ),
    )
    content_kind: str = Field(
        default=CONTENT_KIND_DISCUSS,
        description='"read" (open the article) or "discuss" (open chat with Buddy).',
    )


class UserFramingContext(BaseModel):
    """Compact read-only view the framer sees about the user."""

    top_interests: list[str] = Field(default_factory=list)
    dominant_tone: str | None = None
    user_local_time_band: str = "anytime"   # morning | midday | afternoon | evening | late
    depth_level: int = 1                    # PRODUCT_STRATEGY section 13: 1..5
    # Stored always; influences TONE only — never the topic, never the register,
    # never a stereotype (see plan decision #6). None when not captured.
    gender: str | None = None
    # Language the push copy is written in. Defaults to English; a Hindi/Telugu/
    # Spanish user gets copy in their language.
    language: str = "English"
    # True when top_interests holds specific subjects (e.g. "Verstappen"); False
    # when we only know the broad areas the user picked at signup (e.g. "Sports").
    # A cold-start user with no learned subjects gets a looser, category-level
    # relevance gate so they are not starved until the extractor learns subjects
    # from chat. Defaults True so established users keep the strict subject gate.
    has_specific_interests: bool = True
    # What Buddy calls the user, when known. Optional warmth only: the prompt is
    # told to use it sparingly, never to open every push with it. None when unknown.
    name: str | None = None


def _build_framer_prompt(candidate: ScoredCandidate, user_context: UserFramingContext) -> str:
    return signal_notification_user_prompt(
        name=user_context.name or "",
        interests=user_context.top_interests,
        has_specific_interests=user_context.has_specific_interests,
        tone=user_context.dominant_tone or "",
        language=user_context.language,
        gender=user_context.gender or "",
        time_band=user_context.user_local_time_band,
        depth_level=user_context.depth_level,
        source=candidate.source,
        category=candidate.category,
        title=candidate.title,
        body=candidate.body,
        url=candidate.url,
    )


def _content_kind_for_source(candidate: ScoredCandidate) -> str:
    """Fallback content_kind: anything with a url is readable, otherwise discuss."""
    return CONTENT_KIND_READ if (candidate.url or "").strip() else CONTENT_KIND_DISCUSS


def _safe_fallback(candidate: ScoredCandidate) -> FramedNotification:
    title = strip_long_dashes(
        candidate.title or candidate.source or "Something for you"
    )[:NOTIFICATION_TITLE_MAX_CHARS]
    body = strip_long_dashes(
        f"From {candidate.source}. Worth a look."
        if candidate.source else "Tap to read."
    )[:NOTIFICATION_BODY_MAX_CHARS]
    opening = strip_long_dashes(
        f"Came across this and thought of you: {candidate.title}"
        if candidate.title else "Came across something I thought you might like."
    )[:OPENING_CHAT_MESSAGE_MAX_CHARS]
    # Fail CLOSED but LOUD: a framer outage is an infra failure, not a relevance
    # pass. Rather than fire hollow "from <source>, worth a look" copy (exactly the
    # vapor that prompted this change), defer the send — is_relevant=False with the
    # FRAMER_UNAVAILABLE_REASON sentinel routes the scoring loop to log an outage and
    # retry next tick. content_kind is still inferred so a recovered tick is correct.
    return FramedNotification(
        title=title,
        body=body,
        opening_chat_message=opening,
        is_relevant=False,
        relevance_reason=FRAMER_UNAVAILABLE_REASON,
        content_kind=_content_kind_for_source(candidate),
    )


def _normalise(
    framed: FramedNotification,
    candidate: ScoredCandidate,
    *,
    breaking_news: bool = False,
) -> FramedNotification:
    """Truncate to platform limits and decide content_kind deterministically from
    the candidate, NOT the model: anything with a url opens the article ("read");
    only a urlless item (e.g. a live score) opens chat ("discuss"). The user wants
    article taps to open the source every time, so we never let the model mislabel
    a readable article as "discuss" (the bug that opened chat instead of the piece).

    Breaking news is the one exception: it is companion-first ("discuss") so Buddy
    opens the conversation with the heads-up; the url still rides in the payload for
    an in-chat citation."""
    if breaking_news:
        content_kind = CONTENT_KIND_DISCUSS
    else:
        content_kind = CONTENT_KIND_READ if (candidate.url or "").strip() else CONTENT_KIND_DISCUSS
    return FramedNotification(
        title=truncate_at_word_boundary(
            strip_long_dashes(framed.title), NOTIFICATION_TITLE_MAX_CHARS
        ),
        body=truncate_at_word_boundary(
            strip_long_dashes(framed.body), NOTIFICATION_BODY_MAX_CHARS
        ),
        opening_chat_message=truncate_at_word_boundary(
            strip_long_dashes(framed.opening_chat_message), OPENING_CHAT_MESSAGE_MAX_CHARS
        ),
        is_relevant=framed.is_relevant,
        relevance_reason=framed.relevance_reason[:RELEVANCE_REASON_MAX_CHARS],
        content_kind=content_kind,
    )


async def frame_notification(
    models: ModelProvider,
    candidate: ScoredCandidate,
    user_context: UserFramingContext,
    *,
    breaking_news: bool = False,
) -> FramedNotification:
    """One LLM call. Returns a safe fallback on any failure.

    When ``breaking_news`` is True the relevance gate is NOT applied — scoring's
    salience bar already justified the send — so a dedicated prompt always writes a
    warm heads-up (is_relevant=true) instead of the personal-relevance judgement."""
    prompt = _build_framer_prompt(candidate, user_context)
    system_prompt = (
        BREAKING_SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT
        if breaking_news
        else SIGNAL_NOTIFICATION_FRAMER_SYSTEM_PROMPT
    )
    try:
        result = await models.cheap(
            prompt,
            system=system_prompt,
            response_model=FramedNotification,
            temperature=0.6,
        )
        framed = cast(FramedNotification, result)
        return _normalise(framed, candidate, breaking_news=breaking_news)
    except Exception as exc:
        logger.warn("notification_framer: LLM framing failed, using fallback", {
            "content_id": candidate.content_id,
            "source": candidate.source,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return _safe_fallback(candidate)


def derive_local_time_band(local_datetime: datetime) -> str:
    """Map an hour-of-day to a coarse band the framer can reference."""
    h = local_datetime.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "midday"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "late"


# ── Copy quality linter ───────────────────────────────────────────────────────
# Hard-rule checks for a framed push, factored out so the stress-test harness and
# its unit test share ONE definition of "bad copy". NOT used in the hot path (the
# framer prompt is the primary control); it exists to catch regressions and to
# eyeball what the live model actually produces. Mirrors the NEVER rules in
# buddy_voice.py + the framer prompt so a drift there is caught by the test.
_SOURCE_MENTIONS = (
    "hacker news", "hackernews", "google news", "arxiv", "reddit", "newsdata",
    "an article", "this article", "the article", "a thread", "a post",
)
_LAZY_QUESTIONS = (
    "what do you think", "thoughts?", "what do you make of", "what are your thoughts",
    "have you seen this", "did you see that", "curious about why",
)
# Wire-service and newsletter register: excitement about the SUBJECT with nobody in it.
# These are the phrases the shipped tracker and briefing copy actually used, and they
# are what BUDDY_PUSH_ENERGY exists to replace. Energy itself is fine; this is not.
_WIRE_COPY_PHRASES = (
    "highly anticipated", "get ready for", "the wait is over", "what promises to be",
    "great news", "time to get ready", "find out more", "find out what experts",
    "see what it's doing", "don't miss out", "stay tuned",
)
# Only long dashes are policed in copy. Exclamation marks, hyphens, and double
# hyphens are allowed; the live path also strips long dashes via strip_long_dashes,
# so this linter exists to catch a regression in the framer's own output.
_BANNED_PUNCTUATION = ("—", "–")


def _body_restates_title(title: str, body: str) -> bool:
    """True when the body adds nothing the title did not already carry.

    This is the failure that dominated the shipped tracker copy: title "FIFA World Cup
    2026 Final: Argentina vs Spain", body "FIFA World Cup 2026 Final: Argentina vs Spain
    is underway!". Two lines of notification shade carrying one fact.

    Word-set based rather than substring based, because the real cases re-order and
    re-punctuate rather than repeat verbatim. Short titles are skipped: a 2-word title
    legitimately shares its words with the body.
    """
    title_words = {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2}
    body_words = {w for w in re.findall(r"[a-z0-9]+", body.lower()) if len(w) > 2}
    if len(title_words) < 3 or not body_words:
        return False
    # Every meaningful word of the title reappears in the body, and the body brings
    # fewer than three words of its own: it is a restatement, not a second beat.
    return title_words <= body_words and len(body_words - title_words) < 3


def copy_violations(framed: FramedNotification) -> list[str]:
    """Return hard-rule violations in a framed push. Empty list == clean copy.

    A rejection (is_relevant=false) carries no copy to lint but must still carry a
    reason (the Gate B contract). A relevant push is linted for length, banned
    punctuation, naming the source, and lazy dead-end questions."""
    issues: list[str] = []
    if not framed.is_relevant:
        if not (framed.relevance_reason or "").strip():
            issues.append("rejected without a relevance_reason")
        return issues

    title = framed.title or ""
    body = framed.body or ""
    blob = f"{title}\n{body}\n{framed.opening_chat_message or ''}".lower()

    if len(title) > NOTIFICATION_TITLE_MAX_CHARS:
        issues.append(f"title over {NOTIFICATION_TITLE_MAX_CHARS} chars")
    if len(body) > NOTIFICATION_BODY_MAX_CHARS:
        issues.append(f"body over {NOTIFICATION_BODY_MAX_CHARS} chars")
    if not title.strip() or not body.strip():
        issues.append("relevant push with an empty title or body")
    if not (framed.relevance_reason or "").strip():
        issues.append("relevant push without a relevance_reason")
    for punct in _BANNED_PUNCTUATION:
        if punct in title or punct in body:
            issues.append(f"banned punctuation {punct!r} in title/body")
    for mention in _SOURCE_MENTIONS:
        if mention in blob:
            issues.append(f"names the source/medium ({mention!r})")
    for question in _LAZY_QUESTIONS:
        if question in blob:
            issues.append(f"lazy dead-end question ({question!r})")
    for phrase in _WIRE_COPY_PHRASES:
        if phrase in blob:
            issues.append(f"wire-copy phrase ({phrase!r})")
    if _body_restates_title(title, body):
        issues.append("body restates the title (two lines carrying one fact)")
    if framed.content_kind not in (CONTENT_KIND_READ, CONTENT_KIND_DISCUSS):
        issues.append(f"invalid content_kind {framed.content_kind!r}")
    return issues
