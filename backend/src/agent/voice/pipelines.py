"""Builders for the cascading STT -> LLM -> TTS voice pipeline.

Each provider stack is a FallbackAdapter so a single provider outage degrades
instead of dropping the call. This module owns every `livekit.plugins` import
the worker makes at session-build time (the `silero` VAD prewarm lives in
voice_agent.py); the deps-drift guard test scans this package to keep the
pyproject extras in sync.
"""

from __future__ import annotations

from livekit.agents import NOT_GIVEN, AgentSession, TurnHandlingOptions, mcp
from livekit.agents import llm as lk_llm
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.agents import vad as lk_vad
from livekit.plugins import anthropic, cartesia, deepgram, google, openai
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from ...config.settings import settings
from .fallback_tts_wrapper import SpeechMarkupStrippingTTS


def build_stt_pipeline() -> lk_stt.FallbackAdapter:
    """Deepgram nova-3 with a nova-2 fallback."""
    return lk_stt.FallbackAdapter(
        [
            deepgram.STT(model="nova-3", api_key=settings.DEEPGRAM_API_KEY.strip()),
            deepgram.STT(model="nova-2", api_key=settings.DEEPGRAM_API_KEY.strip()),
        ],
        attempt_timeout=10.0,
        max_retry_per_stt=0,
    )


def build_llm_pipeline(user_id: str) -> lk_llm.FallbackAdapter:
    """OpenAI (if configured) -> Anthropic Claude -> Gemini Flash, in that order."""
    llm_adapters: list[lk_llm.LLM] = []
    if settings.OPENAI_API_KEY:
        # OpenAI caches the longest common prefix automatically (>=1024-token
        # prefix) — no cache_control needed.
        llm_adapters.append(
            openai.LLM(
                model=settings.OPENAI_CHAT_MODEL,
                api_key=settings.OPENAI_API_KEY.strip(),
                prompt_cache_key=user_id,
            )
        )
    # caching="ephemeral" stamps cache_control on the system prompt + tools
    # so the long voice prompt is read from cache on turn 2+ (lower TTFT).
    llm_adapters.append(
        anthropic.LLM(
            model=settings.ANTHROPIC_VOICE_MODEL,
            api_key=settings.ANTHROPIC_API_KEY.strip(),
            caching="ephemeral",
        )
    )
    llm_adapters.append(
        google.LLM(model=settings.TIER_CHEAP, api_key=settings.GEMINI_API_KEY.strip())
    )
    return lk_llm.FallbackAdapter(llm_adapters, attempt_timeout=10.0, max_retry_per_llm=0)


def build_tts_pipeline(sonic3_controls: dict) -> lk_tts.FallbackAdapter:
    """Cartesia sonic-3 (conditioned) -> Deepgram aura-2 -> Cartesia sonic-2.

    Only the sonic-3 primary supports generation_config speed/emotion; the
    Deepgram and sonic-2 fallbacks are left unconditioned. `sonic3_controls` is
    empty for a profile-less user so the primary constructs the exact default voice.

    Both fallbacks are wrapped in SpeechMarkupStrippingTTS: the reply stream can
    carry sonic-3-only inline markup ([laughter], <emotion/speed/volume> tags
    from emotion_tags.py) that these engines would otherwise read aloud as
    literal text.
    """
    return lk_tts.FallbackAdapter(
        [
            cartesia.TTS(
                api_key=settings.CARTESIA_API_KEY.strip(), model="sonic-3", **sonic3_controls
            ),
            SpeechMarkupStrippingTTS(
                deepgram.TTS(model="aura-2-andromeda-en", api_key=settings.DEEPGRAM_API_KEY.strip())
            ),
            SpeechMarkupStrippingTTS(
                cartesia.TTS(api_key=settings.CARTESIA_API_KEY.strip(), model="sonic-2")
            ),
        ],
        max_retry_per_tts=0,
    )


def build_mcp_server(firebase_id_token: str) -> mcp.MCPServerHTTP:
    """MCP tool server at the backend /mcp endpoint, authed with the worker's ID token."""
    mcp_url = f"{settings.BACKEND_INTERNAL_URL.rstrip('/')}/mcp/"
    return mcp.MCPServerHTTP(
        url=mcp_url,
        transport_type="streamable_http",
        headers={"Authorization": f"Bearer {firebase_id_token}"},
    )


def build_turn_detector() -> MultilingualModel:
    """The semantic end-of-turn model. Raises if the ONNX model fails to load."""
    return MultilingualModel()


def build_agent_session(
    *,
    stt: lk_stt.FallbackAdapter,
    llm: lk_llm.FallbackAdapter,
    tts: lk_tts.FallbackAdapter,
    vad: lk_vad.VAD,
    turn_detector: MultilingualModel | None,
    mcp_server: mcp.MCPServerHTTP,
) -> AgentSession:
    """Assemble the AgentSession with tuned turn-handling for snappy, uninterrupted replies.

    `turn_detector` is None when the semantic model failed to load;
    passing NOT_GIVEN makes LiveKit fall back to VAD-based endpointing 
    so the call still works (just without semantic end-of-turn).
    """
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        turn_detection=turn_detector if turn_detector is not None else NOT_GIVEN,
        preemptive_generation=True,
        mcp_servers=[mcp_server], 
        # First silence-presence tier: LiveKit emits "away" after this much user
        # silence, which recorder.py answers with the playful (screen-aware when
        # a fresh frame exists) check-in; the deeper memory-pull tier fires at
        # VOICE_AWAY_SECOND_NUDGE_S total (see voice/recorder.py).
        user_away_timeout=settings.VOICE_AWAY_FIRST_NUDGE_S,
        turn_handling=TurnHandlingOptions(
            # MultilingualModel already guards turn finality semantically. Keep
            # the low floor for confident endings and cap uncertain endings at
            # 0.8s so a false-negative EOU prediction cannot add two seconds.
            endpointing={
                "min_delay": 0.2,
                "max_delay": 0.8,
            },
            interruption={
                "mode": "adaptive",
                # Require 0.5s min duration of speech before an interruption counts,
                # so backchannels ("yeah", "mm-hm") don't cut Buddy off.
                "min_duration": 0.5,
                "false_interruption_timeout": 1.5,
                "resume_false_interruption": True,
            },
        ),
    )
