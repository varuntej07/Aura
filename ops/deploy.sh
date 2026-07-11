#!/usr/bin/env bash
# One-command deploy of the ops dashboard to Cloud Run (service juno-ops), separate from the
# backend. Run from anywhere:   bash ops/deploy.sh [PROJECT] [REGION]
#
# It prompts for a passcode (the only thing it needs from you), deploys the Firestore indexes,
# builds + deploys the service, and prints the URL. Open the URL on any device, type the
# passcode once, done. The service URL is GUESSABLE, so the passcode is the real lock.
set -euo pipefail

PROJECT="${1:-juno-2ea45}"
REGION="${2:-us-central1}"
SERVICE="juno-ops"
IMAGE="gcr.io/${PROJECT}/${SERVICE}:latest"

OPS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$OPS_DIR/.." && pwd)"

# This must run under Git Bash, not WSL. WSL can't see Windows node/firebase/gcloud/docker,
# so firebase dies with "node: not found". Catch it up front instead of failing mid-deploy.
if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "ERROR: this is WSL bash, which can't reach your Windows tools (node/firebase/gcloud)." >&2
  echo "Re-run with Git Bash (same as the backend deploy):" >&2
  echo '  & "C:\Program Files\Git\bin\bash.exe" ops/deploy.sh juno-2ea45 us-central1' >&2
  exit 1
fi

# Optional config from ops/.env (passcode / posthog / offset). Safe if the file is absent.
if [ -f "$OPS_DIR/.env" ]; then set -a; source "$OPS_DIR/.env"; set +a; fi

# Passcode: prompt if not already set in .env. Min 8 chars, letters+numbers (no commas, the
# env-var list below is comma-separated).
if [ -z "${OPS_PASSCODE:-}" ]; then
  read -r -s -p "Set a dashboard passcode (8+ letters/numbers): " OPS_PASSCODE; echo
fi
if [ "${#OPS_PASSCODE}" -lt 8 ]; then
  echo "ERROR: passcode must be at least 8 characters." >&2; exit 1
fi

# Optional least-privilege service account (see prerequisites below). Default SA used if unset.
SA_ARGS=()
if [ -n "${OPS_SERVICE_ACCOUNT:-}" ]; then SA_ARGS+=(--service-account="$OPS_SERVICE_ACCOUNT"); fi

# One-time prerequisites (uncomment to run once for least-privilege; otherwise the default
# Cloud Run service account is used):
#   gcloud iam service-accounts create juno-ops --project="$PROJECT"
#   for ROLE in roles/datastore.viewer roles/monitoring.viewer roles/logging.viewer; do
#     gcloud projects add-iam-policy-binding "$PROJECT" \
#       --member="serviceAccount:juno-ops@${PROJECT}.iam.gserviceaccount.com" --role="$ROLE"; done

# --- Firestore indexes: ONE-TIME setup, NOT part of routine deploys -----------------------
# The message/voice collection-group indexes were deployed once at first setup and rarely
# change. Re-deploying them on every code push is wasteful and needs node/firebase (which
# breaks under WSL). If you EVER change firestore.indexes.json, run this once, by hand:
#   firebase deploy --only firestore:indexes --project juno-2ea45

# --- Build, push, deploy ------------------------------------------------------------------
docker build -t "$IMAGE" "$OPS_DIR"
docker push "$IMAGE"

gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --allow-unauthenticated \
  "${SA_ARGS[@]}" \
  --memory=512Mi \
  --min-instances=0 \
  --set-env-vars="GCP_PROJECT=${PROJECT},OPS_PASSCODE=${OPS_PASSCODE},POSTHOG_HOST=${POSTHOG_HOST:-https://us.i.posthog.com},POSTHOG_PROJECT_ID=${POSTHOG_PROJECT_ID:-},POSTHOG_PERSONAL_KEY=${POSTHOG_PERSONAL_KEY:-},OPS_UTC_OFFSET_HOURS=${OPS_UTC_OFFSET_HOURS:-0},OPS_POSTHOG_WEB_PROJECT_ID=${OPS_POSTHOG_WEB_PROJECT_ID:-},LANGFUSE_HOST=${LANGFUSE_HOST:-https://us.cloud.langfuse.com},LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY:-},LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY:-},SENTRY_ORG=${SENTRY_ORG:-},SENTRY_PROJECT=${SENTRY_PROJECT:-},SENTRY_AUTH_TOKEN=${SENTRY_AUTH_TOKEN:-},GITHUB_TOKEN=${GITHUB_TOKEN:-},OPS_CRASHLYTICS_BQ_DATASET=${OPS_CRASHLYTICS_BQ_DATASET:-firebase_crashlytics}"

URL=$(gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" --format='value(status.url)')
echo ""
echo "Deployed. Open this on any device and enter your passcode:"
echo "  $URL"
