# Aura Backend

FastAPI backend for the Aura/Buddy app, deployed on Google Cloud Run.

## What runs where

- `src/main.py` is the FastAPI HTTP API (uvicorn, Cloud Run)
- `src/agent/voice_agent.py` is the LiveKit voice worker, a separate long-lived process
- `src/handlers/` holds the endpoint logic: chat, reminders, devices, calendar, notifications, signal engine, threads
- `src/services/signal_engine/` is the embedding-driven notification and feed ranking layer (no LLM in the scoring hot path)
- `src/services/briefing/` builds the daily morning digest and the on-demand world snapshot (flag-gated)
- `src/services/` holds the Claude, Gemini, Firebase, FCM, Google Calendar, and Cloud Tasks integrations
- `src/agents/` holds the scheduled data fetchers (Hacker News, arXiv, cricket scores, Google News RSS) plus the tech-news, sports, and posts agents that shape them

Voice stack: LiveKit carries the audio, Deepgram does STT, the LLM is GPT-4.1 mini with Claude then Gemini as fallbacks, Cartesia does TTS, and Silero VAD plus the LiveKit turn detector handle turn taking.

## Run locally

```powershell
cd backend
uvicorn src.main:app --reload --port 8000
```

Voice worker (separate terminal):

```powershell
cd backend
python -m src.agent.voice_agent start
```

## Deploy to Cloud Run

```powershell
gcloud run deploy juno-backend --source backend/ --region us-central1 --project juno-2ea45
```

Existing secrets and env vars on the service are preserved. Cloud Run only updates what you explicitly pass.

## Scheduled jobs (Cloud Scheduler)

| Job | Schedule | Endpoint |
|---|---|---|
| `juno-reminder-tick` | every minute | `/scheduler/tick`, which also runs calendar sync and sports ingest every 30 min |
| `juno-agents-tick` | 09:00 daily | `/internal/agents/tick` for domain agent fetches |
| `juno-daily-notify` | configured | `/internal/daily-notify/send` for calendar meeting reminders |
| signal engine tick | every 15 min | `/internal/signal-engine/tick` |
| content ingest | hourly | `/internal/signal-engine/content-ingest` |

The thread reflector (minute 0), icebreaker (minute 15), and daily-briefing fan-out (minute % 15 == 5) also piggyback `/scheduler/tick` on minute gates rather than their own jobs, and stay no-ops while their feature flags are off.
