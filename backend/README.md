# Aura Backend

FastAPI backend for the Aura/Buddy app, deployed on Google Cloud Run.

## What runs where

- `src/main.py` — FastAPI HTTP API (uvicorn, Cloud Run)
- `src/agent/voice_agent.py` — LiveKit voice worker, separate long-lived process
- `src/handlers/` — endpoint logic: chat, nutrition, reminders, devices, calendar, notifications, signal engine
- `src/services/signal_engine/` — embedding-driven notification and feed ranking layer (no LLM in scoring hot path)
- `src/services/` — Claude, Gemini, Firebase, FCM, Google Calendar, Cloud Tasks integrations
- `src/agents/` — scheduled data fetchers (HN, arXiv, cricket, jobs, sports web search)

Voice stack: LiveKit → Deepgram (STT) → Claude (reasoning) → Cartesia (TTS) → Silero VAD.

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

Existing secrets and env vars on the service are preserved — Cloud Run only updates what you explicitly pass.

## Scheduled jobs (Cloud Scheduler)

| Job | Schedule | Endpoint |
|---|---|---|
| `juno-reminder-tick` | every minute | `/scheduler/tick` — reminders, calendar sync, sports ingest every 30 min |
| `juno-agents-tick` | 09:00 daily | `/internal/agents/tick` — domain agent fetches |
| `juno-daily-notify` | configured | `/internal/daily-notify/send` — calendar meeting reminders |
| signal engine tick | every 15 min | `/internal/signal-engine/tick` |
| content ingest | hourly | `/internal/signal-engine/content-ingest` |
