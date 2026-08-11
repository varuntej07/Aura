"""Meeting Recording V2 ingest, durable processing, and deletion.

The desktop client captures meeting audio locally (WASAPI loopback + mic,
two-channel FLAC segments), claims an installation-owned capture run, uploads
immutable digest-addressed segments, and submits a receipt-verifiable manifest.
Firestore jobs and outbox rows are authoritative; Cloud Tasks only delivers.
Fenced workers persist immutable provider/transcript artifacts, apply the
versioned quality gate, and never delete source audio as a success side effect.
Deletion is an explicit exact-generation saga.

Source architecture: Aura-Desktop/MEETING_RECORDING_V2_ARCHITECTURE.md.
Backend reference: backend/docs/meeting-recording-v2.md.
"""
