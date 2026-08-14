"""The persistence boundary for untrusted text. Where page content stops being bytes.

**This is deliberately NOT a prompt-injection filter, and must never become one.**

The defense against injection in this package is architectural: the reading, verifying
and synthesising stages pass no ``tools=`` argument, so there is no capability present
to hijack, and structured output is the only channel out of a model. A textual filter
adds nothing to that and actively subtracts, because a pattern list creates confidence
proportional to its length rather than to its coverage.

``brave_search._strip_prompt_injection`` exists and is left exactly as it is, applied
only where it already applies (the Brave snippet blob). It removes three literal
line-anchored shapes, which measurably reduces log noise. It does not cover instructions
phrased normally, anything mid-line, non-English phrasing, base64, homoglyphs,
zero-width obfuscation, or anything inside a code block, and it is not applied to page
bodies at all. Extending it here would be a false control.

What this module does instead is narrow and provable: make stored text inert. Control
and bidi characters cannot survive, markdown cannot render, and link targets found
inside page prose are discarded so the only URLs a brief renders are ones our own fetch
path produced.
"""

from __future__ import annotations

import unicodedata

# Hard ceiling matching Evidence.excerpt. Applied after stripping, so an excerpt padded
# with zero-width characters cannot smuggle extra visible content past the bound.
EXCERPT_MAX_CHARS = 400

# Zero-width and bidirectional-override codepoints. These are excluded by name rather
# than by category because they are formatting characters (Cf) that render as nothing
# while changing how everything around them reads. A right-to-left override can make a
# stored excerpt display in an order that reverses its meaning.
_INVISIBLE = frozenset(
    "​‌‍⁠﻿"          # zero-width space/non-joiner/joiner/word-joiner/BOM
    "‎‏"                              # LTR/RTL marks
    "‪‫‬‭‮"           # embedding / override / pop
    "⁦⁧⁨⁩"                 # isolates
    "­"                                    # soft hyphen
)

# Markdown-active characters. Neutralised rather than escaped: an excerpt is displayed
# as plain text, so a literal backtick or bracket carries no meaning worth preserving,
# and escaping would leave backslashes visible in a quoted span.
_MARKDOWN_ACTIVE = str.maketrans({
    "`": "'",
    "*": " ",
    "_": " ",
    "[": "(",
    "]": ")",
    "<": "(",
    ">": ")",
    "|": " ",
    "#": " ",
    "~": " ",
})


def _strip_controls(value: str) -> str:
    """Drop C0/C1 control and invisible formatting characters, keeping real whitespace.

    Tabs and newlines survive this pass and are collapsed later; everything else in the
    control range goes. ``unicodedata.category`` is used rather than a codepoint range so
    the check stays correct for the whole C1 block, which is easy to miss by hand.
    """
    out: list[str] = []
    for char in value:
        if char in _INVISIBLE:
            continue
        if char in ("\t", "\n", "\r"):
            out.append(" ")
            continue
        # Cc = control, Cf = format, Cs = surrogate. None of the three has any business
        # in a stored excerpt; Co (private use) is left alone as it renders as a glyph.
        if unicodedata.category(char) in ("Cc", "Cf", "Cs"):
            continue
        out.append(char)
    return "".join(out)


def _drop_link_targets(tokens: list[str]) -> list[str]:
    """Remove URL-shaped tokens found INSIDE page prose.

    The only URLs a brief renders are ones our own fetch path produced and validated
    against the public-HTTPS policy. A link discovered in page text has passed none of
    those checks, so carrying it into stored evidence would put an unvalidated target in
    front of the user under the authority of a cited brief.

    Token-shaped rather than pattern-matched: anything carrying a scheme separator or a
    bare "www." prefix goes. This is not trying to be a URL parser, and it does not need
    to be, because a false positive costs one word of an excerpt.
    """
    out: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if "://" in lowered or lowered.startswith("www."):
            continue
        out.append(token)
    return out


def plain_text(value: str, *, max_chars: int = EXCERPT_MAX_CHARS) -> str:
    """Make one span of untrusted page text safe to store and render.

    Order matters. Stripping happens BEFORE clamping so padding cannot push visible
    content past the bound, and normalization happens first so a decomposed lookalike is
    folded before anything else inspects it.
    """
    if not value:
        return ""
    # NFKC folds compatibility forms, so full-width and styled lookalikes collapse to
    # their plain equivalents instead of surviving as distinct-looking text.
    text = unicodedata.normalize("NFKC", value)
    text = _strip_controls(text)
    text = text.translate(_MARKDOWN_ACTIVE)
    text = " ".join(_drop_link_targets(text.split()))
    if len(text) > max_chars:
        # Budget the ellipsis INSIDE the bound. Evidence.excerpt is max_length=400, so a
        # clamp that returned 403 would fail validation and lose the whole claim.
        body_max = max(1, max_chars - 3)
        text = text[:body_max]
        # Cut on a word boundary when one is close, so a clamped excerpt reads as a
        # truncated sentence rather than a severed word.
        pivot = text.rfind(" ")
        if pivot > body_max - 40:
            text = text[:pivot]
        text = text.rstrip() + "..."
    return text


def excerpt(value: str) -> str:
    """A verbatim supporting span, made inert. The only text that reaches Evidence."""
    return plain_text(value, max_chars=EXCERPT_MAX_CHARS)


def prose(value: str, *, max_chars: int) -> str:
    """Model-authored narrative (summary, section body, rationale).

    Sanitised on the same path as page text on purpose. A section body is written by our
    own model, but it was written WHILE READING an untrusted page, so treating it as
    trusted because of who produced it would defeat the point of quarantining the input.
    """
    return plain_text(value, max_chars=max_chars)
