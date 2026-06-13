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
#   gcloud secrets create juno-openai-api-key --project=<PROJECT_ID>         # voice worker: primary voice LLM (gpt-4.1-mini)
#   gcloud secrets create juno-gemini-api-key --project=<PROJECT_ID>         # voice worker + signal engine LLM fallback
#   gcloud secrets create juno-brave-api-key --project=<PROJECT_ID>          # backend: real-time web_surf (chat + voice)
#   gcloud secrets create juno-newsdata-api-key --project=<PROJECT_ID>       # backend: signal-engine news pool (newsdata.io)
#
# Cloud Scheduler prerequisite (one-time, NOT created by this script):
#   The juno-scheduler service account must exist, and the Cloud Scheduler
#   service agent (service-<PROJECT_NUMBER>@gcp-sa-cloudscheduler.iam.gserviceaccount.com)
#   needs roles/iam.serviceAccountTokenCreator on it so it can mint the OIDC token
#   the backend's _verify_scheduler_token check requires.
#
# Usage:
#   bash backend/deploy.sh juno-2ea45 us-central1

set -euo pipefail

# Prevent Git Bash on Windows from converting Unix paths inside
# --set-env-vars and --set-secrets args (e.g. /run/secrets/... -> C:/Program Files/Git/run/...).
# The scheduler flags carry slashes too (cron "*/15 ...", time zones "Etc/UTC",
# and the https:// URI/audience), so exclude them as well or the jobs get a
# mangled audience and 401.
# gcloud's own executable path still converts correctly.
export MSYS2_ARG_CONV_EXCL='--set-env-vars;--set-secrets;--schedule;--time-zone;--uri;--oidc-token-audience'

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
  cloudscheduler.googleapis.com \
  --project="${PROJECT_ID}"

# Build & push image
echo "▶ Building Docker image..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -t "${IMAGE}:latest" "${SCRIPT_DIR}"

echo "▶ Pushing image to GCR..."
docker push "${IMAGE}:latest"

# ── OIDC audience contract ───────────────────────────────────────────────────
# Cloud Run serves the backend under a STABLE project-number hostname
# (…-<PROJECT_NUMBER>.<REGION>.run.app) AND a per-service hash hostname
# (…-<hash>-uc.a.run.app, returned by status.url). An OIDC token's 'aud' is
# whichever hostname the caller targeted. We SIGN every scheduler/task token
# with the stable URL and tell the backend to ACCEPT both, so a Cloud Run
# URL-format change can never 401 the scheduler again (the 2026-06-04 outage).
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
STABLE_SERVICE_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
EXISTING_SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true)"
ACCEPTED_AUDIENCES="${STABLE_SERVICE_URL}"
if [[ -n "${EXISTING_SERVICE_URL}" && "${EXISTING_SERVICE_URL}" != "${STABLE_SERVICE_URL}" ]]; then
  ACCEPTED_AUDIENCES="${STABLE_SERVICE_URL} ${EXISTING_SERVICE_URL}"
fi
echo "▶ Stable service URL (token audience): ${STABLE_SERVICE_URL}"
echo "▶ Audiences the backend will accept:   ${ACCEPTED_AUDIENCES}"

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
  --set-env-vars="BACKEND_INTERNAL_URL=${STABLE_SERVICE_URL}" \
  --set-env-vars="SCHEDULER_OIDC_AUDIENCES=${ACCEPTED_AUDIENCES}" \
  --set-secrets="ANTHROPIC_API_KEY=juno-anthropic-api-key:latest" \
  --set-secrets="LIVEKIT_API_KEY=livekit-api-key:latest" \
  --set-secrets="LIVEKIT_API_SECRET=livekit-api-secret:latest" \
  --set-secrets="DEEPGRAM_API_KEY=deepgram-api-key:latest" \
  --set-secrets="CARTESIA_API_KEY=cartesia-api-key:latest" \
  --set-secrets="GOOGLE_CLIENT_ID=juno-google-client-id:latest" \
  --set-secrets="GOOGLE_CLIENT_SECRET=juno-google-client-secret:latest" \
  --set-secrets="GEMINI_API_KEY=juno-gemini-api-key:latest" \
  --set-secrets="BRAVE_API_KEY=juno-brave-api-key:latest" \
  --set-secrets="NEWSDATA_API_KEY=juno-newsdata-api-key:latest" \
  --set-secrets="/run/secrets/service-account.json=juno-firebase-service-account:latest" \
  --set-secrets="LANGFUSE_SECRET_KEY=juno-langfuse-secret-key:latest" \
  --set-env-vars="GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/service-account.json" \
  --set-env-vars="LIVEKIT_URL=${LIVEKIT_URL}" \
  --set-env-vars="LANGFUSE_PUBLIC_KEY=pk-lf-6e4f5a36-9d31-474c-b61a-3307653b6c1d" \
  --set-env-vars="LANGFUSE_HOST=https://hipaa.cloud.langfuse.com" \
  --set-env-vars="POSTHOG_API_KEY=phc_CDtz3DmNraHdnJ2w9W7WJNkJ8VANYPBWAcqV2Uf77k5s" \
  --set-env-vars="POSTHOG_HOST=https://us.i.posthog.com"

# Print service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")

echo ""
echo "✅ ${SERVICE_NAME} deployed: ${SERVICE_URL}"

# Cloud Scheduler jobs — codified so the OIDC audience can never silently drift.
# Each job calls a /scheduler or /internal endpoint guarded by _verify_scheduler_token,
# which checks the OIDC token's audience against settings.scheduler_oidc_audience_list
# (every hostname that routes here). We pin --uri and --oidc-token-audience to the
# STABLE project-number URL (${STABLE_SERVICE_URL}) — never status.url, which switched
# hostname format and caused the 2026-06-04 401 outage. The backend accepts both the
# stable and hash hostnames, so signing with the stable one is always valid.
echo ""
echo "▶ Reconciling Cloud Scheduler jobs (audience=${STABLE_SERVICE_URL})..."
SCHEDULER_SA="juno-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"

ensure_scheduler_job() {
  local name="$1" schedule="$2" path="$3" tz="${4:-Etc/UTC}"
  local args=(
    --location="${REGION}" --project="${PROJECT_ID}"
    --schedule="${schedule}" --time-zone="${tz}"
    --uri="${STABLE_SERVICE_URL}${path}" --http-method=POST
    --oidc-service-account-email="${SCHEDULER_SA}"
    --oidc-token-audience="${STABLE_SERVICE_URL}"
  )
  if gcloud scheduler jobs describe "${name}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  • updating ${name}"
    gcloud scheduler jobs update http "${name}" "${args[@]}"
  else
    echo "  • creating ${name}"
    gcloud scheduler jobs create http "${name}" "${args[@]}"
  fi
}

ensure_scheduler_job "juno-reminder-tick" "* * * * *" "/scheduler/tick"
ensure_scheduler_job "juno-signal-engine-tick" "*/15 * * * *" "/internal/signal-engine/tick"
ensure_scheduler_job "juno-content-ingest" "0 * * * *" "/internal/signal-engine/content-ingest"
ensure_scheduler_job "juno-agents-tick" "0 9 * * *" "/internal/agents/tick" "America/Los_Angeles"

echo "✅ Cloud Scheduler jobs reconciled"

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
  --min-instances=1 \
  --max-instances=2 \
  --memory=4Gi \
  --cpu=2 \
  --no-cpu-throttling \
  --timeout=3600 \
  --concurrency=1 \
  --set-env-vars="ENV=production" \
  --set-env-vars="LIVEKIT_URL=${LIVEKIT_URL}" \
  --set-env-vars="BACKEND_INTERNAL_URL=${STABLE_SERVICE_URL}" \
  --set-secrets="OPENAI_API_KEY=juno-openai-api-key:latest" \
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