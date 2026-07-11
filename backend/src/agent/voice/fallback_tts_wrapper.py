"""A fallback-TTS wrapper that strips sonic-3-only speech markup before speaking.

Only the Cartesia sonic-3 primary understands the inline <emotion/speed/volume>
tags emitted by emotion_tags.py and the [laughter] nonverbalism; Deepgram
aura-2 and Cartesia sonic-2 would read them aloud as literal text. The
FallbackAdapter replays the exact same text chunks to every engine, so the
strip must live on the fallback TTS instances themselves.

streaming=False is deliberate: FallbackAdapter wraps a non-streaming TTS in
its own StreamAdapter, which tokenizes the replayed chunks into COMPLETE
sentences and calls synthesize() per sentence — so this wrapper always sees
whole tags and only ever needs the pure strip. Cost: per-sentence HTTP
synthesis instead of a websocket, on the already-degraded failover path only.

Imports only livekit.agents (no livekit.plugins), so the pyproject
extras-drift guard is unaffected.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents import tts as lk_tts

from .emotion_tags import strip_inline_speech_markup


class SpeechMarkupStrippingTTS(lk_tts.TTS):
    """Delegates to a wrapped TTS with sonic-3 speech markup stripped from the text."""

    def __init__(self, wrapped_tts: lk_tts.TTS) -> None:
        super().__init__(
            capabilities=lk_tts.TTSCapabilities(streaming=False, aligned_transcript=False),
            sample_rate=wrapped_tts.sample_rate,
            num_channels=wrapped_tts.num_channels,
        )
        self._wrapped_tts = wrapped_tts
        # FallbackAdapter subscribes to metrics on ITS instances (this wrapper),
        # so re-emit the wrapped engine's metrics — same pattern as StreamAdapter.
        self._wrapped_tts.on("metrics_collected", self._on_metrics_collected)

    @property
    def model(self) -> str:
        return self._wrapped_tts.model

    @property
    def provider(self) -> str:
        return self._wrapped_tts.provider

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> lk_tts.ChunkedStream:
        return self._wrapped_tts.synthesize(
            strip_inline_speech_markup(text), conn_options=conn_options
        )

    def prewarm(self) -> None:
        self._wrapped_tts.prewarm()

    def _on_metrics_collected(self, *args: Any, **kwargs: Any) -> None:
        self.emit("metrics_collected", *args, **kwargs)

    async def aclose(self) -> None:
        self._wrapped_tts.off("metrics_collected", self._on_metrics_collected)
        await self._wrapped_tts.aclose()
