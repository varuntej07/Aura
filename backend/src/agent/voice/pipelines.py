"""Builders for the cascading STT -> LLM -> TTS voice pipeline.

Each provider stack is a FallbackAdapter so a single provider outage degrades
instead of dropping the call. This module owns every `livekit.plugins` import
the worker makes at session-build time (the `silero` VAD prewarm lives in
voice_agent.py); the deps-drift guard test scans this package to keep the
pyproject extras in sync.
"""

from __future__ import annotations

from livekit.agents import AgentSession, TurnHandlingOptions, inference, mcp
from livekit.agents import llm as lk_llm
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.agents import vad as lk_vad
from livekit.plugins import anthropic, cartesia, deepgram, google, openai

from ...config.settings import settings
from ...shared.tools import openai_function_definition
from .fallback_tts_wrapper import SpeechMarkupStrippingTTS

VOICE_GENERATION_TEMPERATURE = 0.2
VOICE_MAX_OUTPUT_TOKENS = 16_384


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
    """GPT-4.1 via LiveKit Inference -> GPT-4.1 direct -> Claude Haiku -> Gemini Flash."""
    llm_adapters: list[lk_llm.LLM] = []
    if settings.OPENAI_API_KEY:
        # Legs 1 and 2 are the SAME MODEL on purpose — do not "de-duplicate" them.
        # Leg 1 reaches gpt-4.1 through LiveKit Inference (billed and quota'd by
        # LiveKit), leg 2 through our own OpenAI org.
        # Identical model means tool-calling behaviour is unchanged against the
        # MCP surface, while the pair buys real *transport* and *quota*
        # redundancy: our org running out of TPM does not take out leg 1.
        #
        # inference.LLM exposes no generation kwargs of its own; they go through
        # extra_kwargs, typed as ChatCompletionOptions in
        # livekit/agents/inference/llm.py. Verified there that temperature,
        # max_completion_tokens, parallel_tool_calls and prompt_cache_key are all
        # accepted, so this leg keeps full parity with the direct plugin below
        # (prompt_cache_key in particular — without it a ~6.6k-token prompt would
        # pay full prefill every turn and this leg would be the slowest, not the
        # fastest).
        llm_adapters.append(
            inference.LLM(
                model=f"openai/{settings.OPENAI_CHAT_MODEL}",
                extra_kwargs={
                    "temperature": VOICE_GENERATION_TEMPERATURE,
                    "max_completion_tokens": VOICE_MAX_OUTPUT_TOKENS,
                    "parallel_tool_calls": True,
                    "prompt_cache_key": user_id,
                },
            )
        )
        # OpenAI caches the longest common prefix automatically (>=1024-token
        # prefix) — no cache_control needed.
        llm_adapters.append(
            openai.LLM(
                model=settings.OPENAI_CHAT_MODEL,
                api_key=settings.OPENAI_API_KEY.strip(),
                prompt_cache_key=user_id,
                parallel_tool_calls=True,
                temperature=VOICE_GENERATION_TEMPERATURE,
                max_completion_tokens=VOICE_MAX_OUTPUT_TOKENS,
            )
        )
    # caching="ephemeral" stamps cache_control on the system prompt + tools
    # so the long voice prompt is read from cache on turn 2+ (lower TTFT).
    llm_adapters.append(
        anthropic.LLM(
            model=settings.ANTHROPIC_VOICE_MODEL,
            api_key=settings.ANTHROPIC_API_KEY.strip(),
            caching="ephemeral",
            temperature=VOICE_GENERATION_TEMPERATURE,
            max_tokens=VOICE_MAX_OUTPUT_TOKENS,
        )
    )
    llm_adapters.append(
        google.LLM(
            model=settings.TIER_CHEAP,
            api_key=settings.GEMINI_API_KEY.strip(),
            temperature=VOICE_GENERATION_TEMPERATURE,
            max_output_tokens=VOICE_MAX_OUTPUT_TOKENS,
        )
    )
    # attempt_timeout caps how long a HUNG provider stalls the turn before
    # failover. A healthy leg answers well under 1s (measured llm_ttft_ms
    # 846-1982), so 3s is generous; the previous 10s meant one wedged provider
    # could eat ten seconds of a live call. max_retry_per_llm=0 keeps this a
    # true ceiling — with internal retries the wall-clock would be a multiple of
    # it (llm/fallback_adapter.py passes both into the per-attempt conn options).
    return lk_llm.FallbackAdapter(llm_adapters, attempt_timeout=3.0, max_retry_per_llm=0)


def describe_llm_fallback_legs(adapter: lk_llm.FallbackAdapter) -> list[str]:
    """Return the actual configured adapter order."""
    return [f"{instance.provider}:{instance.model}" for instance in adapter._llm_instances]


def build_tts_pipeline(
    sonic3_controls: dict, *, cartesia_voice: str, deepgram_model: str
) -> lk_tts.FallbackAdapter:
    """Cartesia Sonic 3.5 (conditioned) -> Cartesia Sonic 3 -> Deepgram Aura 2.

    Every leg carries the user's chosen voice as far as its provider allows.
    Both Cartesia legs take the same voice id, so a Sonic 3.5 failure keeps
    Buddy sounding like himself; only prosody (speed/emotion, `sonic3_controls`)
    is dropped on the Sonic 3 leg, matching its long-standing unconditioned
    behaviour. `sonic3_controls` is empty for a profile-less user.

    Deepgram cannot express voice, speed, or emotion at all — its plugin takes
    only a model name — so it sits last and merely keeps Buddy talking through a
    Cartesia outage in a coarsely gender-matched Aura-2 voice. It is wrapped in
    SpeechMarkupStrippingTTS because the reply stream can carry Cartesia inline
    markup it would otherwise read aloud; both Cartesia legs understand it.

    Deepgram used to sit second, so that a Cartesia-wide outage failed over in a
    single hop. It was moved last when voices became user-selectable: a transient
    Sonic 3.5 hiccup is far more common than a Cartesia outage, and silently
    swapping a companion's voice mid-call is worse than one extra fast connection
    error on the rare outage path (max_retry_per_tts=0 keeps that hop cheap).
    """
    return lk_tts.FallbackAdapter(
        [
            cartesia.TTS(
                api_key=settings.CARTESIA_API_KEY.strip(),
                model="sonic-3.5",
                voice=cartesia_voice,
                **sonic3_controls,
            ),
            cartesia.TTS(
                api_key=settings.CARTESIA_API_KEY.strip(),
                model="sonic-3",
                voice=cartesia_voice,
            ),
            SpeechMarkupStrippingTTS(
                deepgram.TTS(model=deepgram_model, api_key=settings.DEEPGRAM_API_KEY.strip())
            ),
        ],
        max_retry_per_tts=0,
    )


class AuraMCPServerHTTP(mcp.MCPServerHTTP):
    """Preserve application-owned strict tool contracts in LiveKit's raw schema."""

    def _make_function_tool(self, name, description, input_schema, meta):
        tool = super()._make_function_tool(name, description, input_schema, meta)
        canonical = openai_function_definition(name)
        if canonical and canonical.get("strict") is True:
            tool.info.raw_schema = canonical
        return tool


def build_mcp_server(firebase_id_token: str, session_id: str) -> mcp.MCPServerHTTP:
    """MCP tool server at the backend /mcp endpoint, authed with the worker's ID token."""
    mcp_url = f"{settings.BACKEND_INTERNAL_URL.rstrip('/')}/mcp/"
    return AuraMCPServerHTTP(
        url=mcp_url,
        transport_type="streamable_http",
        headers={
            "Authorization": f"Bearer {firebase_id_token}",
            "X-Aura-Voice-Session": session_id,
        },
    )


def build_turn_detector() -> inference.TurnDetector:
    """The audio end-of-turn model (LiveKit Agents >=1.6.1, built into the SDK).

    Replaces the deprecated livekit-plugins-turn-detector MultilingualModel,
    which is slated for removal in Agents 2.0. Two reasons beyond deprecation:

    - The text model reads the STT *transcript*, so it could only run once
      Deepgram finalised; it sat serialised behind STT. This model encodes the
      user's audio directly (semantics + intonation/pitch/rhythm), so it runs
      concurrently with STT and adds no serial delay to the turn.
    - It removes the transformers / onnxruntime / huggingface-hub dependency
      branch entirely, which is what emitted the `[transformers] PyTorch was not
      found` ERROR on every worker boot.

    Version auto-selects: v1 (full model, served on LiveKit Inference, free for
    agents deployed to LiveKit Cloud — which is us) and falls back to the local
    CPU v1-mini on connection failure or prediction timeout. That fallback is
    sticky for the session and logged once; a fresh session retries v1.
    docs.livekit.io/agents/logic/turns/turn-detector
    """
    return inference.TurnDetector()


def build_agent_session(
    *,
    stt: lk_stt.FallbackAdapter,
    llm: lk_llm.FallbackAdapter,
    tts: lk_tts.FallbackAdapter,
    vad: lk_vad.VAD,
    turn_detector: inference.TurnDetector | None,
    mcp_server: mcp.MCPServerHTTP,
) -> AgentSession:
    """Assemble the AgentSession with tuned turn-handling for snappy, uninterrupted replies.

    `turn_detector` is None when the end-of-turn model failed to construct;
    omitting turn_detection makes LiveKit auto-select (VAD-based endpointing)
    so the call still works, just without semantic end-of-turn.

    Everything below lives in turn_handling rather than as top-level kwargs:
    `preemptive_generation` and `turn_detection` on AgentSession are deprecated
    and removed in Agents 2.0 (the worker logs that warning today). Note that
    the top-level preemptive_generation is typed NotGivenOr[bool] and therefore
    *cannot* express preemptive_tts at all — the dict form is the only way to
    reach it.
    """
    turn_handling = TurnHandlingOptions(
        # Endpointing is BINARY, not a gradient. audio_recognition.py's
        # _run_eou_detection starts at min_delay and switches to max_delay in
        # one branch:
        #
        #   if end_of_turn_probability < unlikely_threshold:
        #       endpointing_delay = self._endpointing.max_delay
        #
        # Nothing lands in between, so max_delay is not a rarely-hit safety
        # ceiling. It is the flat price of every turn the model reads as
        # unfinished. The 2026-08-01 baseline measured exactly that shape:
        # 307/340/345/314/360/346/359 ms on seven turns, and
        # 2500/2501/2500/2500/2501/2500/2501 ms on eight. Eight of fifteen
        # turns paid the whole ceiling, which made endpointing the single
        # largest term in the turn budget, bigger than LLM and TTS combined.
        #
        # The earlier version of this comment said DO NOT lower max_delay. That
        # was written for the TEXT detector, whose end-of-turn call was
        # untrustworthy and really was cutting users off. It no longer
        # describes this pipeline and was actively misleading, but the risk it
        # recorded is real, so it is restated rather than deleted: a low
        # ceiling truncates people mid-thought. What changed is the evidence.
        # A timed-out prediction leaves end_of_turn_probability None and the
        # delay stays at min_delay, so a slow turn cannot be a model failure.
        # The audio model is healthy and deliberately voting "not finished",
        # which makes bounding how long that vote costs a defensible tradeoff
        # rather than papering over a broken signal.
        #
        # mode="dynamic" (DynamicEndpointing in voice/endpointing.py) derives
        # max_delay from an ExpFilter over this user's observed
        # between_turn_delay, bounded by [min_delay, max_delay], so the ceiling
        # converges toward how they actually pause. Two honest limits: alpha
        # defaults to 0.9 and the filter is seeded AT max_delay, so early turns
        # in a session still pay near the ceiling; and 1.0 is a deliberate
        # tradeoff, not a tuned optimum. If users report being cut off, raise
        # this before touching anything else - it is the first suspect, and one
        # number.
        #
        # Why 1.0 and not 1.5: the target is e2e under 1.5s, and endpointing is
        # not the only term. Playback (~50ms measured) and any residual stack on
        # top, so a 1.5s ceiling puts the worst case at ~1.55s and cannot meet
        # the target by construction. 1.0 leaves headroom for the rest of the
        # budget on the turns the model reads as unfinished.
        endpointing={
            "mode": "dynamic",
            "min_delay": 0.3,
            "max_delay": 1.0,
        },
        interruption={
            "mode": "adaptive",
            # Require 0.5s min duration of speech before an interruption counts,
            # so backchannels ("yeah", "mm-hm") don't cut Buddy off.
            "min_duration": 0.5,
            "false_interruption_timeout": 1.5,
            "resume_false_interruption": True,
        },
        # preemptive_tts defaults to False (_PREEMPTIVE_GENERATION_DEFAULTS),
        # so LLM output was being generated ahead of turn confirmation but TTS
        # still waited for it — leaving measured tts_ttfb_ms (169-407) fully on
        # the critical path. Running TTS preemptively too takes it off.
        #
        # Reuse requires FOUR things to still hold at finalization
        # (agent_activity.py, "using preemptive generation"):
        #
        #   preemptive.info.new_transcript == user_message.text_content
        #   preemptive.chat_ctx.is_equivalent(temp_mutable_chat_ctx)
        #   preemptive.tools == self.tools
        #   preemptive.tool_choice == self._tool_choice
        #
        # voice/speculation.py names only what THIS codebase does to the context
        # and tools, so it can only ever explain the middle two. The first is the
        # SDK's own, and it is the one the 2026-08-01 baseline could not account
        # for: intended reuse 77.8%, observed 44.4%, with LiveKit logging
        # "chat context or tools have changed" exactly 6 times against a
        # mutations tuple that was empty. A turn can honestly report `unchanged`
        # and still lose its speculation because the TRANSCRIPT moved.
        #
        # That is why the two knobs below are raised off their defaults.
        # on_preemptive_generation cancels and re-runs the speculation on each
        # transcript update, re-snapshotting the chat context each time, but only
        # max_retries times per turn. At the default 3, a long or disfluent
        # utterance exhausts its retries early and the last speculation is left
        # holding a transcript the user has since talked past. Raising it also
        # helps the second condition for free, since a later re-snapshot picks up
        # an early-injected context block that landed after the previous one.
        # max_speech_duration is the harder cliff: past it there is no
        # speculation at all, and a 10s ceiling excludes exactly the long turns
        # where a cold round trip is most audible.
        #
        # NOT free, and worth sizing before raising further: each retry is a real
        # LLM request, and with preemptive_tts on, a real TTS request too. Most
        # are cancelled mid-stream, but cancelled generations still bill for what
        # they produced. Going 3 -> 6 roughly doubles the worst-case speculative
        # spend per turn, which at hundreds of users is a real line item, not a
        # rounding error. Judge the trade on measured `speculative_reused`, not
        # on the retry count.
        preemptive_generation={
            "enabled": True,
            "preemptive_tts": True,
            "max_retries": 6,
            "max_speech_duration": 20.0,
        },
    )
    # Set the key ONLY when we have a detector. Absent and None are NOT the same
    # thing here: agent_session.py resolves the key with
    # `turn_handling.get("turn_detection", inference.TurnDetector())`, so an
    # absent key auto-constructs the same audio detector we build, whereas an
    # explicit None disables semantic end-of-turn and drops the session to
    # VAD-only. Writing `turn_detection=turn_detector` unconditionally would
    # therefore turn a construction failure into a silent quality regression.
    if turn_detector is not None:
        turn_handling["turn_detection"] = turn_detector
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        mcp_servers=[mcp_server],
        # Explicitly pin the loop ceiling instead of inheriting a dependency
        # default that can change when livekit-agents is upgraded.
        max_tool_steps=3,
        # LiveKit emits "away" after this much user silence. recorder.py owns
        # the one check-in allowed for that continuous silence span.
        user_away_timeout=settings.VOICE_AWAY_FIRST_NUDGE_S,
        turn_handling=turn_handling,
    )
