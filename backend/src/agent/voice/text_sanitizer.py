"""Deterministic markdown -> speech sanitizer for the voice TTS path.

The LLM (gpt-4.1-mini) frequently emits markdown (bold, bullet lists, headers,
fences) even on a voice call. Cartesia reads that markup literally ("asterisk
asterisk content"), which is the single worst voice-register failure. This module
strips formatting BEFORE text reaches TTS, leaving the spoken words intact.

`sanitize_for_speech` is a pure, deterministic function (easy to unit-test).
`sanitize_text_stream` wraps the streaming text the TTS node receives, flushing on
sentence boundaries so synthesis stays incremental.

Design rules:
- Strip emphasis/bold/headers/bullets/fences/links, KEEP the inner words.
- Never strip underscores inside identifiers (snake_case like get_user_context) or
  hyphens inside words: only paired emphasis delimiters at word boundaries go.
- The literal WORD "asterisk" is letters, never a `*` character, so it always
  survives; only the `*` symbol is removed.
- Fail open: any error returns the original text rather than dropping the turn.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

# Fenced code block markers (```), with an optional language tag, removed line-wise.
_FENCE = re.compile(r"```[^\n`]*\n?")
# Images before links so the alt text wins; both keep the label, drop the URL.
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
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
