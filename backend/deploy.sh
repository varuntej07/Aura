#!/usr/bin/env bash
# deploy.sh — Build and deploy Juno backend + voice worker to Google Cloud Run
#
# Prerequisites (run once):
#   1. Install gcloud CLI: https://cloud.google.com/sdk/docs/install
#   2. gcloud auth login
#   3. gcloud auth configure-docker
#   4. Create secrets in GCP Secret Manager for all required API keys and credentials
#
# Secrets to create before first deploy:
#   gcloud secrets create juno-anthropic-api-key --project=<PROJECT_ID>
#   gcloud secrets create livekit-api-key --project=<PROJECT_ID>
#   gcloud secrets create livekit-api-secret --project=<PROJECT_ID>
#   gcloud secrets create deepgram-api-key --project=<PROJECT_ID>
#   gcloud secrets create cartesia-api-key --project=<PROJECT_ID>
#   gcloud secrets create juno-google-client-id --project=<PROJECT_ID>
#   gcloud secrets create juno-google-client-secret --project=<PROJECT_ID>
#   gcloud secrets create juno-firebase-service-account --project=<PROJECT_ID>
#   gcloud secrets create juno-firebase-web-api-key --project=<PROJECT_ID>   # voice worker: mint ID tokens for /mcp
#   gcloud secrets create juno-gemini-api-key --project=<PROJECT_ID>         # voice worker + signal engine LLM fallback
#
# Usage:
#   bash backend/deploy.sh juno-2ea45 us-central1

set -euo pipefail

# Prevent Git Bash on Windows from converting Unix paths inside 
# --set-env-vars and --set-secrets args (e.g. /run/secrets/... -> C:/Program Files/Git/run/...).
# gcloud's own executable path still converts correctly.
export MSYS2_ARG_CONV_EXCL='--set-env-vars;--set-secrets'

# Config
PROJECT_ID="${1:?Usage: deploy.sh <GCP_PROJECT_ID> <REGION>}"
REGION="${2:-us-central1}"
SERVICE_NAME="juno-backend"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"
WORKER_SERVICE_NAME="juno-voice-worker"
WORKER_IMAGE="gcr.io/${PROJECT_ID}/${WORKER_SERVICE_NAME}"
LIVEKIT_URL="wss://aura-i06eolmd.livekit.cloud"

echo "▶ Deploying ${SERVICE_NAME} + ${WORKER_SERVICE_NAME} to project=${PROJECT_ID} region=${REGION}"

# Enable required APIs (idempotent)
echo "▶ Enabling GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT_ID}"

# Build & push image
echo "▶ Building Docker image..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -t "${IMAGE}:latest" "${SCRIPT_DIR}"

echo "▶ Pushing image to GCR..."
docker push "${IMAGE}:latest"

# Deploy to Cloud Run
echo "▶ Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}:latest" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --platform=managed \
  --allow-unauthenticated \
  --min-instances=1 \
  --max-instances=3 \
  --memory=1Gi \
  --cpu=1 \
  --timeout=3600 \
  --concurrency=80 \
  --set-env-vars="ENV=production" \
  --set-env-vars="ANTHROPIC_MODEL=claude-sonnet-4-6" \
  --set-env-vars="ANTHROPIC_MAX_TOKENS=8096" \
  --set-env-vars="GOOGLE_REDIRECT_URI=" \
  --set-secrets="ANTHROPIC_API_KEY=juno-anthropic-api-key:latest" \
  --set-secrets="LIVEKIT_API_KEY=livekit-api-key:latest" \
  --set-secrets="LIVEKIT_API_SECRET=livekit-api-secret:latest" \
  --set-secrets="DEEPGRAM_API_KEY=deepgram-api-key:latest" \
  --set-secrets="CARTESIA_API_KEY=cartesia-api-key:latest" \
  --set-secrets="GOOGLE_CLIENT_ID=juno-google-client-id:latest" \
  --set-secrets="GOOGLE_CLIENT_SECRET=juno-google-client-secret:latest" \
  --set-secrets="/run/secrets/service-account.json=juno-firebase-service-account:latest" \
  --set-env-vars="GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/service-account.json" \
  --set-env-vars="LIVEKIT_URL=${LIVEKIT_URL}"

# Print service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")

echo ""
echo "✅ ${SERVICE_NAME} deployed: ${SERVICE_URL}"

# Voice worker
echo ""
echo "▶ Building voice worker Docker image..."
docker build -f "${SCRIPT_DIR}/Dockerfile.worker" -t "${WORKER_IMAGE}:latest" "${SCRIPT_DIR}"

echo "▶ Pushing voice worker image to GCR..."
docker push "${WORKER_IMAGE}:latest"

echo "▶ Deploying voice worker to Cloud Run..."
gcloud run deploy "${WORKER_SERVICE_NAME}" \
  --image="${WORKER_IMAGE}:latest" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --platform=managed \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --memory=4Gi \
  --cpu=2 \
  --no-cpu-throttling \
  --timeout=3600 \
  --concurrency=1 \
  --set-env-vars="ENV=production" \
  --set-env-vars="LIVEKIT_URL=${LIVEKIT_URL}" \
  --set-env-vars="BACKEND_INTERNAL_URL=${SERVICE_URL}" \
  --set-secrets="ANTHROPIC_API_KEY=juno-anthropic-api-key:latest" \
  --set-secrets="LIVEKIT_API_KEY=livekit-api-key:latest" \
  --set-secrets="LIVEKIT_API_SECRET=livekit-api-secret:latest" \
  --set-secrets="DEEPGRAM_API_KEY=deepgram-api-key:latest" \
  --set-secrets="CARTESIA_API_KEY=cartesia-api-key:latest" \
  --set-secrets="FIREBASE_WEB_API_KEY=juno-firebase-web-api-key:latest" \
  --set-secrets="GEMINI_API_KEY=juno-gemini-api-key:latest" \
  --set-secrets="/run/secrets/service-account.json=juno-firebase-service-account:latest" \
  --set-env-vars="GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/service-account.json"

WORKER_URL=$(gcloud run services describe "${WORKER_SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")

echo ""
echo "✅ ${WORKER_SERVICE_NAME} deployed: ${WORKER_URL}"