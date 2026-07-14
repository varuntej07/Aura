"""Copy-quality contract for signal-engine notification framing.

Pure unit tests (no model, no network): they lock the hard-rule linter and the
deterministic _normalise so a regression in the framer's output rules — naming the
source, a lazy "what do you think", over-length copy, or mislabeling a readable
article as a chat — fails CI instead of reaching a user. Mirrors the NEVER rules in
buddy_voice.py and the framer prompt.
"""

from datetime import UTC, datetime

from src.services.signal_engine.content_pool import ScoredCandidate
from src.services.signal_engine.notification_framer import (
    CONTENT_KIND_DISCUSS,
    CONTENT_KIND_READ,
    NOTIFICATION_BODY_MAX_CHARS,
    NOTIFICATION_TITLE_MAX_CHARS,
    FramedNotification,
    _normalise,
    copy_violations,
    truncate_at_word_boundary,
)


def _candidate(url: str = "https://example.com/a") -> ScoredCandidate:
    return ScoredCandidate(
        content_id="google_news_abc",
        source="google_news",
        category="sports",
        title="Verstappen wins Monaco after a late safety car",
        body="A real summary with the decisive final-laps detail.",
        url=url,
        embedding=[0.0],
        freshness_ts=datetime.now(UTC),
        cosine_similarity=0.7,
    )


def test_clean_relevant_push_has_no_violations():
    framed = FramedNotification(
        title="okay this one's actually for you",
        body="Verstappen pulled something off in the last three laps that nobody saw. peek?",
        opening_chat_message="Verstappen just took Monaco after a late safety-car restart. want the key moments?",
        is_relevant=True,
        relevance_reason="A Monaco GP result about Verstappen, a direct match for the user's Formula 1 interest.",
        content_kind=CONTENT_KIND_READ,
    )
    assert copy_violations(framed) == []


def test_the_every_frame_perfect_failure_is_caught():
    """The exact shape of the bad push received in production must be flagged."""
    framed = FramedNotification(
        title="every frame perfect",
        body="this hacker news article is about achieving perfect frames. what do you think?",
        opening_chat_message="this hacker news article is about achieving perfect frames. what do you think?",
        is_relevant=True,
        relevance_reason="tech",
        content_kind=CONTENT_KIND_DISCUSS,
    )
    issues = copy_violations(framed)
    assert any("source" in i for i in issues), issues
    assert any("lazy" in i for i in issues), issues


def test_overlength_copy_is_flagged():
    framed = FramedNotification(
        title="x" * (NOTIFICATION_TITLE_MAX_CHARS + 5),
        body="y" * (NOTIFICATION_BODY_MAX_CHARS + 5),
        opening_chat_message="ok",
        is_relevant=True,
        relevance_reason="a real reason that names the subject and why it matches",
        content_kind=CONTENT_KIND_READ,
    )
    issues = copy_violations(framed)
    assert any("title over" in i for i in issues), issues
    assert any("body over" in i for i in issues), issues


def test_em_dash_is_flagged():
    framed = FramedNotification(
        title="big news today, you will want this",
        body="something happened — and you should know about it. go look",
        opening_chat_message="hey",
        is_relevant=True,
        relevance_reason="a reason that names the subject",
        content_kind=CONTENT_KIND_READ,
    )
    assert any("banned punctuation" in i for i in copy_violations(framed))


def test_exclamation_is_allowed():
    """Only long dashes are policed now; exclamation marks must pass clean."""
    framed = FramedNotification(
        title="big news today!",
        body="something happened that you should know about. go look!",
        opening_chat_message="hey",
        is_relevant=True,
        relevance_reason="a reason that names the subject",
        content_kind=CONTENT_KIND_READ,
    )
    assert not any("banned punctuation" in i for i in copy_violations(framed))


def test_normalise_strips_long_dashes():
    """The live path must guarantee no em/en dash reaches the user, even if the
    model slips one through. Hyphens are intentionally preserved."""
    raw = FramedNotification(
        title="okay this one — it's for you",
        body="Verstappen pulled it off — in the last laps. peek?",
        opening_chat_message="he won Monaco — after a late safety car. want it?",
        is_relevant=True,
        relevance_reason="a reason that names the subject",
        content_kind=CONTENT_KIND_READ,
    )
    normalised = _normalise(raw, _candidate(url="https://example.com/x"))
    assert "—" not in normalised.title
    assert "—" not in normalised.body
    assert "—" not in normalised.opening_chat_message
    assert normalised.title == "okay this one, it's for you"


def test_rejection_requires_a_reason():
    no_reason = FramedNotification(
        title="", body="", opening_chat_message="",
        is_relevant=False, relevance_reason="", content_kind=CONTENT_KIND_DISCUSS,
    )
    assert any("without a relevance_reason" in i for i in copy_violations(no_reason))

    with_reason = FramedNotification(
        title="", body="", opening_chat_message="",
        is_relevant=False,
        relevance_reason="No overlap with the user's named interests; only shares the broad tech tag.",
        content_kind=CONTENT_KIND_DISCUSS,
    )
    assert copy_violations(with_reason) == []


def test_normalise_enforces_length_and_url_routing():
    raw = FramedNotification(
        title="t" * 200,
        body="b" * 400,
        opening_chat_message="o" * 600,
        is_relevant=True,
        relevance_reason="r" * 600,
        content_kind=CONTENT_KIND_DISCUSS,  # model said discuss...
    )
    # ...but the candidate has a url, so a non-breaking item must become "read".
    normalised = _normalise(raw, _candidate(url="https://example.com/x"))
    assert len(normalised.title) <= NOTIFICATION_TITLE_MAX_CHARS
    assert len(normalised.body) <= NOTIFICATION_BODY_MAX_CHARS
    assert normalised.content_kind == CONTENT_KIND_READ


def test_truncate_never_cuts_mid_word():
    """Regression: a body over the cap was sliced mid-token ("...for you" ->
    "...for y") and shipped to the shade. Truncation must land on a word boundary
    and never end with a half word."""
    over = "saw a piece on how trainium is picking up steam in ml workloads, curious if this shifts anything for you"
    out = truncate_at_word_boundary(over, NOTIFICATION_BODY_MAX_CHARS)
    assert len(out) <= NOTIFICATION_BODY_MAX_CHARS
    assert out.endswith("…")
    # the last real word is whole, not a fragment like "y" or "yo"
    assert out.rstrip("…").split()[-1] == "anything"


def test_truncate_leaves_short_copy_untouched():
    fits = "short and complete"
    assert truncate_at_word_boundary(fits, NOTIFICATION_BODY_MAX_CHARS) == fits


def test_normalise_truncates_overlength_body_on_word_boundary():
    raw = FramedNotification(
        title="t",
        body="trainium chips are picking up real momentum in machine learning workloads and that could matter for you",
        opening_chat_message="o",
        is_relevant=True,
        relevance_reason="a reason that names the subject",
        content_kind=CONTENT_KIND_READ,
    )
    normalised = _normalise(raw, _candidate(url="https://example.com/x"))
    assert len(normalised.body) <= NOTIFICATION_BODY_MAX_CHARS
    # no dangling single/double-letter fragment at the end
    last_word = normalised.body.rstrip("…").split()[-1]
    assert len(last_word) > 2


def test_normalise_urlless_item_is_discuss():
    raw = FramedNotification(
        title="t", body="b", opening_chat_message="o",
        is_relevant=True, relevance_reason="r", content_kind=CONTENT_KIND_READ,
    )
    normalised = _normalise(raw, _candidate(url=""))
    assert normalised.content_kind == CONTENT_KIND_DISCUSS


def test_normalise_breaking_is_always_discuss():
    raw = FramedNotification(
        title="t", body="b", opening_chat_message="o",
        is_relevant=True, relevance_reason="r", content_kind=CONTENT_KIND_READ,
    )
    normalised = _normalise(raw, _candidate(url="https://example.com/x"), breaking_news=True)
    assert normalised.content_kind == CONTENT_KIND_DISCUSS
