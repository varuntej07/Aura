#!/bin/bash
# Starts both FastAPI and the LiveKit agent worker in the same container.
# If either process exits, the container exits and Cloud Run restarts it.
set -e

echo "Starting Aura backend..."

# FastAPI REST server
uvicorn src.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
FASTAPI_PID=$!
echo "FastAPI started (PID $FASTAPI_PID)"

# LiveKit agent worker — connects to LiveKit Cloud, waits for voice rooms
python -m src.agent.voice_agent start &
AGENT_PID=$!
echo "LiveKit agent worker started (PID $AGENT_PID)"

# Exit when either process exits (Cloud Run will restart the container)
wait -n $FASTAPI_PID $AGENT_PID
EXIT_CODE=$?
echo "A process exited with code $EXIT_CODE — shutting down"
exit $EXIT_CODE
