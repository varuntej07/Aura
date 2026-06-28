# Aura

Aura is a voice-first personal AI companion. The assistant persona is **Buddy**, built to feel like a close friend who is genuinely curious about you: warm, proactive, and always learning what matters to you.

## What Aura can do

- **Talk in real-time voice or text.** Hold a natural spoken conversation with Buddy, or type, with the same persona and the same tools behind both paths.
- **Remember you.** Buddy builds a passive profile of your interests and what you care about, and carries that context across conversations so it feels like one ongoing relationship.
- **Keep you on track.** Set reminders and ask Buddy to keep you posted on any topic you care about.
- **Reach out first.** Proactive notifications, curiosity-driven follow-up questions, a daily evening briefing, and life-aware openers, all timed to be useful rather than noisy.
- **Search the live web.** When something could have changed since training, Buddy looks it up and answers from the result instead of guessing.
- **Handle your calendar and email.** Buddy works with Google Calendar and Gmail on your behalf.

## Voice architecture

Voice is a separate LiveKit Agents worker, not part of the FastAPI request path. It runs as its own service so a slow or failing voice session never blocks the rest of the app.

**Pipeline.** The worker uses a cascading architecture: speech to text, then language model, then text to speech.

- **STT**: Deepgram Nova, with `nova-3` falling back to `nova-2`
- **LLM**: OpenAI GPT-4.1 mini, falling back to Anthropic Claude, then Gemini Flash (`build_llm_pipeline`)
- **TTS**: Cartesia `sonic-3` (conditioned), falling back to Deepgram `aura-2`, then Cartesia `sonic-2`
- **Turn taking**: Silero VAD plus the LiveKit multilingual turn detector

**Connection flow.** The Flutter client requests a LiveKit room token, then joins room `voice-{uid}`. The worker is waiting on LiveKit Cloud, sees the participant join, and starts a session. Audio flows over WebRTC the whole time.

**Tools.** The agent does not embed its own tools. It pulls them from the backend over MCP using `livekit.agents.mcp.MCPServerHTTP`, authenticating with a short-lived Firebase ID token it mints per session from the user's uid. This means voice and text chat share one tool implementation.

**Module layout.** `voice_agent.py` is a thin orchestrator; its pieces live in `backend/src/agent/voice/`: structured session telemetry, pipeline error classification, Firestore context fetchers, prompt assembly, the STT/LLM/TTS/MCP/turn-detector pipeline builders, per-user voice conditioning, session event recording, and per-session Firebase token minting.

**Failure handling.** Two watchdogs cover the dangerous case where the agent connects but never speaks, for example when LLM credit runs out.

- The Flutter client arms a 15 second silence watchdog when the agent joins and after each user turn. Any sign of life (agent state, audio, text, or data) resets it. On timeout it emits a coded `session.error` that maps to friendly copy, and the mic orb becomes the retry button.
- The backend publishes a `session.error` down the LiveKit data channel on pipeline failure, so the client does not have to wait on its own timer. `classify_pipeline_error` separates provider-exhausted or quota errors from generic failures.

Voice telemetry fires PostHog `voice_first_response` on success and `voice_error` with a code on failure. The backend also logs structured `VoiceSession` lines to Cloud Logging.
