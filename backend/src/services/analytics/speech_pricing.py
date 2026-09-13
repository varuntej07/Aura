"""Estimated speech (STT/TTS) prices for voice session telemetry.

The sibling ``llm_pricing`` module cannot express these. It prices per million
TOKENS; speech vendors bill per audio MINUTE (Deepgram) and per CHARACTER
(Cartesia), so a shared table would have to lie about one of them.

Same contract as ``llm_pricing`` otherwise: these are ESTIMATES for spend
visibility, never billing truth, and an unrecognised provider still gets its
usage counted while the cost estimate stays 0, with one warn per process so a
new vendor shows up in logs rather than silently pricing at zero.

Why this exists at all: voice cost was previously LLM-only. Deepgram and
Cartesia were invisible to both Langfuse and the Firestore ledger, so any
"is voice worth what it costs" answer was missing both speech legs of a
cascading pipeline.

Rates verified 2026-09-12 against each vendor's public pricing page.
"""

from __future__ import annotations

from ...lib.logger import logger

# USD per audio MINUTE of streaming speech-to-text, keyed by provider.
#
# Deepgram lists Nova-3 Monolingual streaming at a $0.0077/min regular price with
# a $0.0048/min promotional price running now. The REGULAR price is used
# deliberately: a promo that lapses would silently under-report every voice
# session, and over-estimating spend is the safer direction for a number used to
# decide whether to keep renting a provider.
_STT_USD_PER_MINUTE: dict[str, float] = {
    "deepgram": 0.0077,
}

# USD per CHARACTER of synthesised speech, keyed by provider.
#
# Cartesia bills in credits, not dollars, and the credit rate is plan-dependent:
# the Pro plan is $5/month for 100,000 credits, at roughly 1 credit per
# character, giving $0.00005/character. CHANGING CARTESIA PLAN CHANGES THIS
# NUMBER and nothing in this repo will notice, so re-derive it from the current
# plan's credit allowance whenever the subscription changes.
_TTS_USD_PER_CHARACTER: dict[str, float] = {
    "cartesia": 0.00005,
}

_warned_unknown_providers: set[str] = set()


def _warn_once(kind: str, provider: str) -> None:
    key = f"{kind}:{provider}"
    if key in _warned_unknown_providers:
        return
    _warned_unknown_providers.add(key)
    logger.warn(
        "speech_pricing: unpriced provider, usage counted but cost estimated at 0",
        {"kind": kind, "provider": provider},
    )


def estimate_stt_microusd(provider: str, audio_seconds: float) -> int:
    """Cost in micro-USD for ``audio_seconds`` of streaming transcription.

    Returns 0 for an unknown provider or non-positive duration, never raises.
    """
    normalized = (provider or "").strip().casefold()
    try:
        seconds = float(audio_seconds or 0.0)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    rate = _STT_USD_PER_MINUTE.get(normalized)
    if rate is None:
        _warn_once("stt", normalized or "<unset>")
        return 0
    return int(round(seconds / 60.0 * rate * 1_000_000))


def estimate_tts_microusd(provider: str, characters: int) -> int:
    """Cost in micro-USD for ``characters`` of synthesised speech.

    Characters rather than audio seconds because that is the unit Cartesia
    actually bills, and a cancelled utterance is charged for what was
    synthesised, not for what the user heard.
    """
    normalized = (provider or "").strip().casefold()
    try:
        count = int(characters or 0)
    except (TypeError, ValueError):
        return 0
    if count <= 0:
        return 0
    rate = _TTS_USD_PER_CHARACTER.get(normalized)
    if rate is None:
        _warn_once("tts", normalized or "<unset>")
        return 0
    return int(round(count * rate * 1_000_000))
