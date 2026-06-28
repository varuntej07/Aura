# Voice Agent — talking to Buddy out loud

**One line:** real-time spoken conversation with Buddy, running as its own always-listening
service, with the same persona and memory as text chat but tuned for a live back-and-forth.

**Why it exists:** voice is the most natural way to have a companion. It is also the feature
the beta is built around (accountability check-ins), so it has to feel immediate and never
just go silent on you.

---

## How a voice call flows

```
you tap the mic
      │
      ▼
you join a live voice room; Buddy (a separate voice worker) joins and greets you
      │
      ▼
you speak → speech-to-text → Buddy thinks (with your memory + a COMPACT digest) → speaks back
      │                                                                              │
      └──────────────────────────── turn by turn ───────────────────────────────────┘
      │
      ▼
you hang up; the full conversation is saved, and a short reflection updates your aura
```

---

## The context trick (why it stays sharp)

A voice prompt is built ONCE at the start of the call and rides every turn. If you stuff the
whole history in there, the model drifts. So:

```
LIVE PROMPT  =  persona + rules + a COMPACT digest of what matters (kept small)
FULL HISTORY →  saved in the archive (nothing is lost)
NEED AN OLD DETAIL?  →  Buddy fetches it on demand with a tool, instead of carrying it all hot
```

Leaner live prompt means more room to actually converse AND better rule-following.

---

## Two worked examples

```
EXAMPLE 1 — the "connected but never speaks" safety net
   you join → Buddy should greet you within a few seconds
        ▼
   a silence watchdog (15s) is armed when Buddy joins, and after each of your turns
        ▼
   if Buddy goes silent (e.g. the language model ran out of credit), the watchdog fires
        ▼
   you get a friendly, coded error and the mic orb becomes a retry button,
   instead of staring at a dead screen waiting forever
```

```
EXAMPLE 2 — the free-tier "1 minute left" warning, done gracefully
   the server (NOT the model) owns the countdown
        ▼
   at T-60s it injects a one-shot instruction: "mention ~1 min left warmly, then keep talking"
        ▼
   Buddy weaves it in at the next natural pause, in its own voice, without cutting you off
        ▼
   at T-0 it queues ONE graceful closing line, THEN ends, never a hard cut to silence
```

---

## Guardrails

```
▸ lean live prompt + an async digest built off your turn (never recomputed mid-reply)
▸ the watchdog covers the silent-hang; both client and server can raise a coded error
▸ errors are mapped to friendly copy; success/failure are tracked for monitoring
```

---

## Where this connects

Voice shares Buddy's persona and reads/writes the same [aura profile](./user-aura.md) as
[chat](./chat-and-tools.md). Feedback you say out loud routes through the
[feedback relay](./feedback-relay.md). It runs as a separate worker (it is a live audio
pipeline, not a request/response endpoint).

---

## Code map (for engineers)

```
backend/src/agent/
  voice_agent.py     the thin orchestrator + the worker entrypoint
  voice/             the pieces: pipelines, prompt context, error mapping, the session recorder
  voice_prompt.py    the VOICE_PROMPT persona + rules
backend/src/services/voice_session_summarizer.py   the after-call reflection into the aura
```

The voice worker runs on LiveKit Cloud Agents (it was migrated off an always-on server to
cut idle cost). Telemetry and friendly error copy are described in CLAUDE.md's voice section.
