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

from datetime import datetime
from typing import cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..buddy_voice import BUDDY_CONTENT_PUSH_RULES, BUDDY_VOICE_CORE
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


_FRAMER_SYSTEM_PROMPT = f"""\
{BUDDY_VOICE_CORE}

{BUDDY_CONTENT_PUSH_RULES}

THE TASK
You are writing a single push notification to one specific user. Scoring already
chose the content and the moment. Your job is the words AND a relevance judgement.

Format, all hard:
- title: at most 50 characters, sentence case, no emojis, no exclamation marks.
- body: at most 100 characters, one short sentence that opens a curiosity loop.
- opening_chat_message: one or two sentences Buddy says IF the chat opens (the no-url
  fallback path). Reference the content concretely, still in your own voice.
- gender is provided for natural tone only. It must NEVER change the topic, the
  register, or introduce any gender stereotype. Copy for the same content must be
  the same regardless of gender. Do not mention or imply the user's gender.

Relevance (is_relevant + relevance_reason), the hard gate:
- Set is_relevant=true ONLY if you can name the specific interest or subject this
  content matches for THIS user (e.g. "names Verstappen, matches Formula 1"). Put
  that in relevance_reason. A shared broad category is NOT enough: an item that is
  merely tagged the same bucket as the user's interest, with no concrete subject in
  common, is is_relevant=false.
- If the content body has no real substance to assess (e.g. only engagement counts
  like "120 points, 60 comments", no article text), set is_relevant=false.
- relevance_reason is REQUIRED whenever is_relevant=true. Write it as ONE full
  plain-language sentence (not a couple of words) that names the specific interest
  or subject this content matches and why it is a fit for THIS user. An empty or
  vague reason means the notification will NOT be sent.
- When you reject, relevance_reason is still a full sentence saying plainly why it
  does not match the user's named interests. Do not pad.

content_kind:
- "read" if the item is an article/news/paper the user would open and read (it has
  a url). This is the default for anything with a url.
- "discuss" only when there is nothing to open (e.g. a live score, no url).

Examples:

1) Relevant article, obsessed-friend voice, opens a loop (names the subject):
USER top_interests: Formula 1, Verstappen, KCR
CONTENT source: google_news, category: sports, title: "Verstappen wins Monaco GP after late safety car", body: "<real summary>"
{{"title":"okay this one's so you","body":"Verstappen pulled something off at Monaco in the last laps and I had to flag it. Peek?","opening_chat_message":"Verstappen just won Monaco after a late safety-car restart and held everyone off. Want the key moments?","is_relevant":true,"relevance_reason":"This is a race result about Max Verstappen winning the Monaco Grand Prix, which is a direct match for the user's stated interest in Formula 1 and Verstappen specifically.","content_kind":"read"}}

2) Reject, off-topic item only sharing a broad tag:
USER top_interests: Formula 1, Verstappen, KCR
CONTENT source: google_news, category: tech, title: "A new productivity app promises to fix your focus", body: "<a generic startup launch write-up>"
{{"title":"","body":"","opening_chat_message":"","is_relevant":false,"relevance_reason":"This is a generic productivity-app launch with no connection to the user's named interests in Formula 1, Verstappen, or KCR; it only shares the broad 'tech' tag.","content_kind":"discuss"}}

3) Reject, no substance to assess:
USER top_interests: cricket, KCR
CONTENT source: google_news, category: news, title: "Travel locally, where you are", body: ""
{{"title":"","body":"","opening_chat_message":"","is_relevant":false,"relevance_reason":"This is a travel piece that matches none of the user's named interests in cricket or KCR, and it carries no article text to judge relevance against.","content_kind":"discuss"}}

Output ONLY valid JSON matching the schema. No markdown fences. No prose.

Schema:
{{
  "title": "string",
  "body": "string",
  "opening_chat_message": "string",
  "is_relevant": true,
  "relevance_reason": "string",
  "content_kind": "read" | "discuss"
}}
"""


_BREAKING_FRAMER_SYSTEM_PROMPT = f"""\
{BUDDY_VOICE_CORE}

{BUDDY_CONTENT_PUSH_RULES}

THE TASK
You are writing ONE push notification about a GENUINELY MAJOR, worldwide breaking
news story. Scoring already decided this story is globally important enough that
EVERY user should hear about it — even if it is outside their usual interests. Do
NOT judge personal relevance here; your only job is warm, exciting heads-up copy
that makes the user glad Buddy told them.

Format, all hard:
- title: at most 50 characters, sentence case, no emojis, no exclamation marks.
- body: at most 100 characters, one short sentence that conveys why this is big and
  opens a curiosity loop.
- opening_chat_message: one or two sentences Buddy says when the chat opens — share
  the news concretely, in your own voice, like a friend who just had to tell them.
- Always set is_relevant=true and content_kind="discuss".
- relevance_reason: one short sentence noting this is globally significant breaking
  news everyone should know.

Output ONLY valid JSON matching the schema. No markdown fences. No prose.

Schema:
{{
  "title": "string",
  "body": "string",
  "opening_chat_message": "string",
  "is_relevant": true,
  "relevance_reason": "string",
  "content_kind": "discuss"
}}
"""


def _build_framer_prompt(candidate: ScoredCandidate, user_context: UserFramingContext) -> str:
    interests_line = (
        ", ".join(user_context.top_interests[:3])
        if user_context.top_interests else "no strong interests recorded yet"
    )
    tone_line = user_context.dominant_tone or "neutral"
    gender_line = user_context.gender or "unspecified"
    return f"""\
            USER CONTEXT
            top_interests: {interests_line}
            dominant_tone: {tone_line}
            language: {user_context.language}
            gender: {gender_line}
            local_time_band: {user_context.user_local_time_band}
            depth_level: {user_context.depth_level}

            CONTENT
            source: {candidate.source}
            category: {candidate.category}
            title: {candidate.title}
            body: {candidate.body[:400]}
            url: {candidate.url}

            Write the notification now. JSON only.
        """


def _content_kind_for_source(candidate: ScoredCandidate) -> str:
    """Fallback content_kind: anything with a url is readable, otherwise discuss."""
    return CONTENT_KIND_READ if (candidate.url or "").strip() else CONTENT_KIND_DISCUSS


def _safe_fallback(candidate: ScoredCandidate) -> FramedNotification:
    title = (candidate.title or candidate.source or "Something for you")[:NOTIFICATION_TITLE_MAX_CHARS]
    body = (
        f"From {candidate.source}. Worth a look."
        if candidate.source else "Tap to read."
    )[:NOTIFICATION_BODY_MAX_CHARS]
    opening = (
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
        title=framed.title[:NOTIFICATION_TITLE_MAX_CHARS],
        body=framed.body[:NOTIFICATION_BODY_MAX_CHARS],
        opening_chat_message=framed.opening_chat_message[:OPENING_CHAT_MESSAGE_MAX_CHARS],
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
    system_prompt = _BREAKING_FRAMER_SYSTEM_PROMPT if breaking_news else _FRAMER_SYSTEM_PROMPT
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
