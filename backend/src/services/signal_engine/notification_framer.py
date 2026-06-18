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

# Stamped onto every framed notification's ledger row. Bump this whenever
# _FRAMER_SYSTEM_PROMPT changes so a tap-rate shift can be attributed to a
# specific copy revision (the A/B hook for "what phrasing gets the click").
FRAMER_PROMPT_VERSION = "2026-06-17"

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
- NEVER end the body or the opener with "what do you think?", "thoughts?", or "what
  do you make of this?". Those are dead-end questions that kill the tap. The invite
  is light and forward instead: "peek?", "worth two minutes", "go look".
- NEVER restate the title back to them ("this article is about X"). React to it like
  a friend who knows they care, and keep the actual payoff behind the tap.
- You MAY use their name occasionally for warmth when it lands naturally, but do NOT
  open every push with it. Most pushes should not use the name at all.

Relevance (is_relevant + relevance_reason), the hard gate:
- Set is_relevant=true ONLY if you can name the specific interest or subject this
  content matches for THIS user (e.g. "names Verstappen, matches Formula 1"). Put
  that in relevance_reason. A shared broad category is NOT enough: an item that is
  merely tagged the same bucket as the user's interest, with no concrete subject in
  common, is is_relevant=false.
- COLD-START EXCEPTION: when the USER CONTEXT says their interests are "broad areas
  they picked at signup, no specific subjects learned yet", a clear, confident match
  to one of those areas IS relevant. Name the area in relevance_reason ("a cricket
  series result, and they picked Sports at signup"). The "broad category is not
  enough" rule applies only once specific subjects are known. Reject items with no
  real substance regardless of cold-start.
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

4) Cold-start user, only broad declared areas, a confident category match is fine:
USER top_interests (broad areas they picked at signup, no specific subjects learned yet): Sports, Technology, News
CONTENT source: newsdata, category: sports, title: "India chase down 320 in the final over to take the series", body: "<real match summary with the chase detail>"
{{"title":"your team did not make that easy","body":"it came down to the very last over and the way it ended is a little ridiculous. take a look","opening_chat_message":"India just chased 320 and sealed the series in the final over. want how the chase actually went down?","is_relevant":true,"relevance_reason":"A cricket series-decider result, and the user picked Sports at signup with no narrower subject learned yet, so a confident Sports match applies.","content_kind":"read"}}

5) Relevant, specific subject, the opener withholds the payoff:
USER top_interests: Tesla, EVs, KCR
CONTENT source: newsdata, category: business, title: "Tesla quietly cuts Model Y price in two markets", body: "<real summary with the figure>"
{{"title":"Tesla just moved on the Model Y","body":"a quiet price change in two markets that buyers are going to feel. worth two minutes","opening_chat_message":"Tesla cut the Model Y price in two markets out of nowhere. want the numbers and which ones?","is_relevant":true,"relevance_reason":"A Tesla Model Y pricing change, a direct match for the user's stated interest in Tesla and EVs.","content_kind":"read"}}

NEVER WRITE LIKE THESE (the exact failures this prompt exists to kill):
- "this hacker news article 'every frame perfect' is about achieving perfect frames. what do you think?"  -> names the source, restates the title, closes the loop, dead-end question.
- "Found an active article. Might be useful."  -> filler, no specific subject, no hook.
- "Big news in tech today!"  -> exclamation, generic, names no specific thing.

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
        if user_context.top_interests else "none recorded yet"
    )
    interest_kind = (
        "specific subjects they care about"
        if user_context.has_specific_interests
        else "broad areas they picked at signup, no specific subjects learned yet"
    )
    tone_line = user_context.dominant_tone or "neutral"
    gender_line = user_context.gender or "unspecified"
    name_line = user_context.name or "unknown"
    return f"""\
            USER CONTEXT
            name: {name_line}
            top_interests ({interest_kind}): {interests_line}
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
        title=strip_long_dashes(framed.title)[:NOTIFICATION_TITLE_MAX_CHARS],
        body=strip_long_dashes(framed.body)[:NOTIFICATION_BODY_MAX_CHARS],
        opening_chat_message=strip_long_dashes(
            framed.opening_chat_message
        )[:OPENING_CHAT_MESSAGE_MAX_CHARS],
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
)
# Only long dashes are policed in copy. Exclamation marks, hyphens, and double
# hyphens are allowed; the live path also strips long dashes via strip_long_dashes,
# so this linter exists to catch a regression in the framer's own output.
_BANNED_PUNCTUATION = ("—", "–")


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
    if framed.content_kind not in (CONTENT_KIND_READ, CONTENT_KIND_DISCUSS):
        issues.append(f"invalid content_kind {framed.content_kind!r}")
    return issues
