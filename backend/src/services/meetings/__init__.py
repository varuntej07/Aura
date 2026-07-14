"""Meeting notes - capture ingest, storage, and post-meeting synthesis.

The desktop client captures meeting audio locally (WASAPI loopback + mic,
2-channel FLAC segments), claims a meeting slot against the monthly cap,
uploads segments, and marks the capture complete. A Cloud Tasks job then
transcribes (Deepgram multichannel), synthesizes a note (LLM), persists it
to Firestore with tier-conditional retention, and deletes the raw audio
immediately. Design doc: Aura-Desktop/MEETING_NOTES_PLAN.md.
"""
