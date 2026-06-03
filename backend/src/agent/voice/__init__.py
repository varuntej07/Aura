"""Voice worker support package.

The runnable worker entrypoint stays at `src/agent/voice_agent.py` (invoked as
`python -m src.agent.voice_agent start`). This package holds the pieces that
entrypoint composes: telemetry, error mapping, Firestore fetchers, prompt
context assembly, the STT/LLM/TTS pipeline builders, per-session voice
conditioning, and the session event recorder.
"""
