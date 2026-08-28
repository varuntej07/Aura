"""Deterministic written-text -> spoken-language sanitizer for voice TTS.

The LLM (gpt-4.1-mini) frequently emits markdown (bold, bullet lists, headers,
fences) even on a voice call. Cartesia reads that markup literally ("asterisk
asterisk content"), which is the single worst voice-register failure. This module
strips formatting BEFORE text reaches TTS, then rewrites written-only forms
(numeric dates, URLs, regex escapes, identifiers, and symbols) into language a
person would actually say.

`sanitize_for_speech` is a pure, deterministic function (easy to unit-test).
`sanitize_text_stream` wraps the streaming text the TTS node receives, flushing on
sentence boundaries so synthesis stays incremental.

Design rules:
- Strip emphasis/bold/headers/bullets/fences/links, KEEP the inner words.
- Convert identifiers and hyphenated written forms to ordinary spoken words.
- Render known initialisms the way people actually say them: letter-spaced
  ("A P I"), never expanded into a phrase nobody speaks.
- Render dates, times, money, percentages, decimals, and ordinals in words.
- Replace unhelpful technical notation with its conversational referent.
- The literal WORD "asterisk" is letters, never a `*` character, so it always
  survives; only the `*` symbol is removed.
- Fail open: any error returns the original text rather than dropping the turn.
"""

from __future__ import annotations

import re
from calendar import month_name
from collections.abc import AsyncIterable, AsyncIterator

# Fenced code block markers (```), with an optional language tag, removed line-wise.
_FENCE = re.compile(r"```[^\n`]*\n?")
# Images before links so the alt text wins; both keep the label, drop the URL.
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Raw URLs and email addresses are never useful speech. There is no implied
# screen on a mobile call, so use auditory referents rather than "the link on
# screen" when a model ignores the voice prompt.
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\]]+", re.IGNORECASE)
_BARE_WEB_ADDRESS = re.compile(
    r"\b(?:www\.)?[A-Z0-9-]+\.(?:ai|app|co|com|dev|edu|gov|io|net|org)"
    r"(?:/[^\s<>\]]*)?",
    re.IGNORECASE,
)
# Written technical forms that should be described, not dictated character by
# character. Dates are normalized before paths so 8/9/2026 is not mistaken for
# a slash-delimited location.
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\s,;:]+\\?)+")
_REGEX_ESCAPE_RUN = re.compile(r"(?:\\[A-Za-z0-9])+(?:[+*?{}()\[\]|.^$-]*)")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_SLASH_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")
_TIME = re.compile(
    r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?:\s*([ap])\.?m\.?)?(?!\d)",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?([2-9]\d{2})\)?[ .-](\d{3})[ .-](\d{4})(?!\d)")
_CURRENCY = re.compile(r"(?<!\w)([$€£])(\d[\d,]*)(?:\.(\d{1,2}))?")
_PERCENT = re.compile(r"(?<!\w)(-?\d+(?:\.\d+)?)\s*%")
_ORDINAL = re.compile(r"(?<!\w)(-?\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_DECIMAL = re.compile(r"(?<![\w.])(-?\d+)\.(\d+)(?![\w.])")
_INTEGER = re.compile(r"(?<![\w.])-?\d[\d,]*(?![\w.])")
_SNAKE_CASE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")
_ALL_CAPS_WORD = re.compile(r"\b[A-Z]{2,}\b")
_WORD_HYPHEN = re.compile(r"(?<=[A-Za-z0-9])-(?=[A-Za-z0-9])")
# Bold/strong first, so the inner ** pairs are consumed before single-* italic runs.
_BOLD_STAR = re.compile(r"\*\*([^\n]+?)\*\*")
_BOLD_UNDER = re.compile(r"__([^\n]+?)__")
# Line-anchored block markup (multiline): ATX headers, bullets, ordered lists, quotes.
_HEADER = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]*")
_BULLET = re.compile(r"(?m)^[ \t]*(?:[*\-+]|\d+[.)])[ \t]+")
_BLOCKQUOTE = re.compile(r"(?m)^[ \t]*>[ \t]?")
# Italic emphasis. `*word*` is safe to strip; `_word_` only when NOT inside an
# identifier (negative lookarounds exclude word chars and underscores), so
# snake_case survives.
_ITALIC_STAR = re.compile(r"\*([^*\n]+?)\*")
_ITALIC_UNDER = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])")
# Any leftover asterisks (stray bold/bullet remnants, a literal `*` symbol). We do
# NOT strip stray underscores or hyphens, to protect identifiers and hyphenated words.
_STRAY_STAR = re.compile(r"\*+")
_MULTISPACE = re.compile(r"[ \t]{2,}")
_MULTINEWLINE = re.compile(r"\n{3,}")

_SPOKEN_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\be\.g\.(?=\s|$)", re.IGNORECASE), "for example"),
    (re.compile(r"\bi\.e\.(?=\s|$)", re.IGNORECASE), "that is"),
    (re.compile(r"\betc\.(?=\s|$)", re.IGNORECASE), "and so on"),
    (re.compile(r"\bvs\.(?=\s|$)", re.IGNORECASE), "versus"),
    (re.compile(r"\bU\.S\.(?=\s|$)", re.IGNORECASE), "United States"),
    (re.compile(r"\bU\.K\.(?=\s|$)", re.IGNORECASE), "United Kingdom"),
)

# Initialisms people say as letters. Rendered letter-spaced ("A P I") so every
# engine reads them as letters; single capitals never match _ALL_CAPS_WORD, so
# the later lowercasing pass cannot mangle them. Only tokens that are
# unambiguous as an all-caps word belong here: "IT", "PM", "OK", "PIN" and
# similar are ordinary English in caps somewhere and must stay out. Pronounced
# acronyms ("JSON", "NASA") also stay out; the lowercasing pass already yields
# a speakable word for those.
_SPELLED_INITIALISMS: frozenset[str] = frozenset(
    {
        "ADHD",
        "AI",
        "API",
        "CPU",
        "ETA",
        "FAQ",
        "GPS",
        "GPU",
        "HTML",
        "HTTP",
        "HTTPS",
        "ID",
        "LLM",
        "PDF",
        "SQL",
        "STT",
        "TTS",
        "UI",
        "URL",
        "UX",
    }
)

# The rare initialisms whose expansion IS how people say them.
_INITIALISM_WORD_FORMS: dict[str, str] = {
    "US": "United States",
}

_INITIALISM_PATTERN = re.compile(
    r"\b("
    + "|".join(
        sorted(
            (re.escape(token) for token in _SPELLED_INITIALISMS | set(_INITIALISM_WORD_FORMS)),
            key=len,
            reverse=True,
        )
    )
    + r")(s?)\b"
)

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ORDINAL_SMALL = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
}
_ORDINAL_TENS = {
    20: "twentieth",
    30: "thirtieth",
    40: "fortieth",
    50: "fiftieth",
    60: "sixtieth",
    70: "seventieth",
    80: "eightieth",
    90: "ninetieth",
}


def _integer_words(value: int) -> str:
    """Render an integer as plain English without pronunciation-hostile hyphens."""
    if value < 0:
        return f"negative {_integer_words(-value)}"
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, remainder = divmod(value, 10)
        return _TENS[tens] if not remainder else f"{_TENS[tens]} {_ONES[remainder]}"
    if value < 1_000:
        hundreds, remainder = divmod(value, 100)
        head = f"{_ONES[hundreds]} hundred"
        return head if not remainder else f"{head} {_integer_words(remainder)}"
    for scale, label in ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")):
        if value >= scale:
            major, remainder = divmod(value, scale)
            head = f"{_integer_words(major)} {label}"
            return head if not remainder else f"{head} {_integer_words(remainder)}"
    return str(value)


def _ordinal_words(value: int) -> str:
    if value < 0:
        return f"negative {_ordinal_words(-value)}"
    if value in _ORDINAL_SMALL:
        return _ORDINAL_SMALL[value]
    if value in _ORDINAL_TENS:
        return _ORDINAL_TENS[value]
    if value < 100:
        tens, remainder = divmod(value, 10)
        return f"{_TENS[tens]} {_ORDINAL_SMALL[remainder]}"
    # Dates only need 1..31. For larger ordinals, retain the cardinal meaning
    # and add a natural ordinal suffix instead of risking a malformed word.
    return f"{_integer_words(value)}th"


def _year_words(year: int) -> str:
    if 2000 <= year <= 2009:
        return "two thousand" if year == 2000 else f"two thousand {_integer_words(year - 2000)}"
    if 2010 <= year <= 2099:
        return f"twenty {_integer_words(year - 2000)}"
    return _integer_words(year)


def _date_words(year: int, month: int, day: int, original: str) -> str:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return original
    return f"{month_name[month]} {_ordinal_words(day)}, {_year_words(year)}"


def _replace_iso_date(match: re.Match[str]) -> str:
    year, month, day = (int(part) for part in match.groups())
    return _date_words(year, month, day, match.group(0))


def _replace_slash_date(match: re.Match[str]) -> str:
    # The active English voice is en-US. Slash dates therefore follow the US
    # month/day/year convention; the authored prompt is required to avoid this
    # ambiguous written form before this fallback is needed.
    month, day, year = (int(part) for part in match.groups())
    return _date_words(year, month, day, match.group(0))


def _replace_time(match: re.Match[str]) -> str:
    original = match.group(0)
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = (match.group(3) or "").lower()
    if meridiem:
        if hour == 12 and minute == 0:
            spoken = "noon" if meridiem == "p" else "midnight"
            return f"{spoken}." if original.endswith(".") else spoken
        spoken_hour = 12 if hour == 0 else hour
        period = "in the morning" if meridiem == "a" else "in the evening"
    else:
        spoken_hour = hour
        period = ""
    if minute == 0:
        spoken = _integer_words(spoken_hour)
    elif minute < 10:
        spoken = f"{_integer_words(spoken_hour)} oh {_integer_words(minute)}"
    else:
        spoken = f"{_integer_words(spoken_hour)} {_integer_words(minute)}"
    spoken = f"{spoken} {period}".strip()
    return f"{spoken}." if original.endswith(".") else spoken


def _replace_phone(match: re.Match[str]) -> str:
    def digits(group: str) -> str:
        return " ".join(_ONES[int(char)] for char in group)

    return f"{digits(match.group(1))}, {digits(match.group(2))}, {digits(match.group(3))}"


def _replace_currency(match: re.Match[str]) -> str:
    symbol, whole_raw, cents_raw = match.groups()
    whole = int(whole_raw.replace(",", ""))
    unit, singular = {"$": ("dollars", "dollar"), "€": ("euros", "euro"), "£": ("pounds", "pound")}[
        symbol
    ]
    spoken = f"{_integer_words(whole)} {singular if whole == 1 else unit}"
    if cents_raw:
        cents = int(cents_raw.ljust(2, "0"))
        if cents:
            spoken += f" and {_integer_words(cents)} {'cent' if cents == 1 else 'cents'}"
    return spoken


def _replace_decimal_text(value: str) -> str:
    whole, fraction = value.split(".", 1)
    return f"{_integer_words(int(whole))} point {' '.join(_ONES[int(char)] for char in fraction)}"


def _replace_percent(match: re.Match[str]) -> str:
    value = match.group(1)
    spoken = _replace_decimal_text(value) if "." in value else _integer_words(int(value))
    return f"{spoken} percent"


def _replace_initialism(match: re.Match[str]) -> str:
    token, plural_s = match.group(1), match.group(2)
    word_form = _INITIALISM_WORD_FORMS.get(token)
    if word_form is not None:
        # "United States" absorbs a stray plural s; nothing sensible pluralizes it.
        return word_form
    spelled = " ".join(token)
    # "A P I's" reads as "ay pee eyes"; a bare trailing s would glue to the last letter.
    return f"{spelled}'s" if plural_s else spelled


def _replace_web_address(match: re.Match[str]) -> str:
    """Keep sentence punctuation outside the written address."""
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,!?":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    return f"the website{trailing}"


def _normalize_written_forms(text: str) -> str:
    """Turn display-oriented notation into natural en-US spoken language."""
    s = _EMAIL.sub("an email address", text)
    s = _URL.sub(_replace_web_address, s)
    s = _BARE_WEB_ADDRESS.sub(_replace_web_address, s)
    s = _ISO_DATE.sub(_replace_iso_date, s)
    s = _SLASH_DATE.sub(_replace_slash_date, s)
    s = _WINDOWS_PATH.sub("that file location", s)
    s = _REGEX_ESCAPE_RUN.sub("that pattern", s)
    s = re.sub(r"(?:that pattern)+", "that pattern", s)
    s = _PHONE.sub(_replace_phone, s)
    s = _TIME.sub(_replace_time, s)
    s = _CURRENCY.sub(_replace_currency, s)
    s = _PERCENT.sub(_replace_percent, s)
    s = _ORDINAL.sub(lambda m: _ordinal_words(int(m.group(1))), s)
    s = _DECIMAL.sub(lambda m: _replace_decimal_text(f"{m.group(1)}.{m.group(2)}"), s)
    s = _INTEGER.sub(lambda m: _integer_words(int(m.group(0).replace(",", ""))), s)
    for pattern, replacement in _SPOKEN_REPLACEMENTS:
        s = pattern.sub(replacement, s)
    s = _SNAKE_CASE.sub(lambda m: m.group(0).replace("_", " "), s)
    s = re.sub(
        r"\b[Vv](\d+)\b",
        lambda m: f"version {_integer_words(int(m.group(1)))}",
        s,
    )
    s = _INITIALISM_PATTERN.sub(_replace_initialism, s)
    # All-caps emphasis makes some engines spell ordinary words letter by
    # letter. Known initialisms were letter-spaced above; lowercase any remainder.
    s = _ALL_CAPS_WORD.sub(lambda m: m.group(0).lower(), s)
    s = _WORD_HYPHEN.sub(" ", s)
    s = re.sub(r"\band\s*/\s*or\b", "or", s, flags=re.IGNORECASE)
    s = re.sub(r"\bw\s*/\s*o\b", "without", s, flags=re.IGNORECASE)
    s = re.sub(r"\bw\s*/\s*", "with ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+/\s+", " or ", s)
    s = s.replace("/", " ").replace("\\", " ")
    s = s.replace(";", ",").replace("&", " and ")
    s = re.sub(r"[|{}<>~^]", " ", s)
    return s


# Written forms this module does not merely tidy but REPLACES: a URL becomes
# "the website", a path becomes "that file location", a regex run becomes "that
# pattern". By the time such text reaches TTS the thing the user asked for is
# gone, so no amount of guessing at the REQUEST wording can rescue it. Backticks
# and fences are included because the model only reaches for them around exact
# content. Anything matching here has to be shown, not spoken.
def sanitize_for_speech(text: str) -> str:
    """Strip markdown formatting from text so it reads cleanly through TTS.

    Pure and deterministic. Returns the original text unchanged on any internal error.
    """
    if not text:
        return text
    try:
        s = text
        s = _FENCE.sub("", s)
        s = s.replace("`", "")
        s = _IMAGE.sub(r"\1", s)
        s = _LINK.sub(r"\1", s)
        s = _BOLD_STAR.sub(r"\1", s)
        s = _BOLD_UNDER.sub(r"\1", s)
        s = _HEADER.sub("", s)
        s = _BULLET.sub("", s)
        s = _BLOCKQUOTE.sub("", s)
        s = _ITALIC_STAR.sub(r"\1", s)
        s = _ITALIC_UNDER.sub(r"\1", s)
        s = _STRAY_STAR.sub("", s)
        s = _normalize_written_forms(s)
        s = _MULTISPACE.sub(" ", s)
        s = _MULTINEWLINE.sub("\n\n", s)
        s = "\n".join(line.strip() for line in s.split("\n"))
        return s.strip()
    except Exception:
        return text


# Bracketed non-verbal cues like [laughter] or [soft laughter]. Cartesia speaks
# ONLY the exact cue [laughter] (see voice_prompt.py); every cue is an
# audio-path instruction that must never show up in the caption or the recorded
# transcript. Matches letters + spaces inside the brackets, so it never touches
# a [POINT:...] tag (digits/colons; already stripped upstream in llm_node) or a
# numeric footnote like "[1]".
_NONVERBAL_CUE = re.compile(r"\[[A-Za-z][A-Za-z ]*\]")


def strip_nonverbal_cues(text: str) -> str:
    """Remove bracketed non-verbal cues (e.g. [laughter]) from display/record text.

    Pure and deterministic. Returns the original text unchanged on any internal
    error. Collapses the double space a mid-sentence cue leaves behind.
    """
    if not text:
        return text
    try:
        s = _NONVERBAL_CUE.sub("", text)
        s = _MULTISPACE.sub(" ", s)
        return s.strip()
    except Exception:
        return text


def bracket_cue_holdback_index(text: str) -> int:
    """Index up to which ``text`` is safe to emit without splitting a cue.

    A trailing unclosed ``[`` whose tail could still grow into a cue (only
    letters/spaces so far) is held back; anything else emits. Mirrors
    point_tag.holdback_start for the [laughter] grammar. Shared with
    emotion_tags.convert_audio_cue_stream: one bracket-cue grammar, one holdback.
    """
    idx = text.rfind("[")
    if idx == -1:
        return len(text)
    tail = text[idx:]
    if "]" in tail:
        return len(text)  # any complete cue was already removed; remainder is safe
    inner = tail[1:]
    if inner == "" or re.fullmatch(r"[A-Za-z ]*", inner):
        return idx  # could still close into a cue - wait for more chunks
    return len(text)


async def strip_nonverbal_cue_stream(
    text_stream: AsyncIterable[str],
) -> AsyncIterator[str]:
    """Strip [laughter]-style cues from a streaming transcript.

    Buffers across chunk boundaries so a cue split as "[laug" + "hter]" is still
    caught before it reaches the client caption. Fail-open: a bare/unterminated
    "[..." left at stream end is emitted as-is (it was never a real cue).
    """
    pending = ""
    last_char = " "  # treat the stream start as a boundary: never open with a space
    async for chunk in text_stream:
        if not isinstance(chunk, str):
            if pending:
                yield pending
                last_char = pending[-1]
                pending = ""
            yield chunk
            continue
        # Remove complete cues and collapse the gap they leave inside this buffer.
        pending = _MULTISPACE.sub(" ", _NONVERBAL_CUE.sub("", pending + chunk))
        cut = bracket_cue_holdback_index(pending)
        emit, pending = pending[:cut], pending[cut:]
        if last_char == " " and emit.startswith(" "):
            # A cue removed at a chunk boundary would otherwise double the space.
            emit = emit.lstrip(" ")
        if emit:
            last_char = emit[-1]
            yield emit
    if pending:
        if last_char == " ":
            pending = pending.lstrip(" ")
        if pending:
            yield pending


# Sentence-ish flush boundaries: synthesize a chunk as soon as a sentence closes so
# TTS stays incremental instead of waiting for the whole reply.
_FLUSH_SEPARATORS = (". ", "! ", "? ", ".\n", "!\n", "?\n", "\n")


async def sanitize_text_stream(text_stream: AsyncIterable[str]) -> AsyncIterator[str]:
    """Wrap the TTS text stream, sanitizing each sentence as it completes.

    Buffers incoming chunks until a sentence boundary, sanitizes that segment, and
    yields it. A markdown delimiter split across a flush boundary still ends up clean
    because the stray-`*` strip runs on every segment (we only lose the emphasis, which
    is being removed anyway). Any trailing buffer is sanitized and yielded at stream end.
    """
    buffer = ""
    async for chunk in text_stream:
        buffer += chunk
        flush_at = -1
        for sep in _FLUSH_SEPARATORS:
            idx = buffer.rfind(sep)
            if idx != -1:
                flush_at = max(flush_at, idx + len(sep))
        if flush_at > 0:
            head, buffer = buffer[:flush_at], buffer[flush_at:]
            cleaned = sanitize_for_speech(head)
            if cleaned:
                yield cleaned + " "
    tail = sanitize_for_speech(buffer)
    if tail:
        yield tail
