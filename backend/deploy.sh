#!/usr/bin/env bash
# deploy.sh — Build and deploy the Juno backend to Google Cloud Run.
# (The voice worker now runs on LiveKit Cloud Agents, not Cloud Run 
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
#   gcloud secrets create juno-dodo-api-key --project=<PROJECT_ID>           # billing: Dodo Payments API key (checkout + portal)
#   gcloud secrets create juno-dodo-webhook-secret --project=<PROJECT_ID>    # billing: Dodo webhook signature secret (whsec_...)
#
# Cloud Scheduler prerequisite (one-time, NOT created by this script):
#   The juno-scheduler service account must exist, and the Cloud Scheduler
#   service agent (service-<PROJECT_NUMBER>@gcp-sa-cloudscheduler.iam.gserviceaccount.com)
#   needs roles/iam.serviceAccountTokenCreator on it so it can mint the OIDC token
#   the backend's _verify_scheduler_token check requires.
#
# Cloud Tasks prerequisite (one-time, already provisioned): the juno-engagement
#   queue (settings.CLOUD_TASKS_QUEUE) carries the engagement, chat-completion,
#   AND the ingest-triggered signal-scoring tasks; no new queue is needed.
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
LIVEKIT_URL="wss://aura-i06eolmd.livekit.cloud"

# Dodo Payments (billing). Test-mode base until launch; flip to
# https://live.dodopayments.com in the Phase 5 launch sequence. The four product
# IDs come from the Dodo dashboard after merchant onboarding; while they are
# empty the billing routes answer 503 billing_not_configured, which is safe.
# The API key and webhook secret ride in via Secret Manager, see the
# commented --set-secrets lines in the deploy block below.
DODO_API_BASE="https://test.dodopayments.com"
DODO_PRODUCT_COMPANION_MONTHLY=""
DODO_PRODUCT_COMPANION_YEARLY=""
DODO_PRODUCT_PRO_MONTHLY=""
DODO_PRODUCT_PRO_YEARLY=""

echo "▶ Deploying ${SERVICE_NAME} to project=${PROJECT_ID} region=${REGION}"

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
docker build -f "${SCRIPT_DIR}/Dockerfile.api" -t "${IMAGE}:latest" "${SCRIPT_DIR}"

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
  --min-instances=0 \
  --max-instances=3 \
  --memory=1Gi \
  --cpu=1 \
  --timeout=3600 \
  --concurrency=80 \
  --set-env-vars="ENV=production" \
  --set-env-vars="ANTHROPIC_CHAT_MODEL=claude-sonnet-4-6" \
  --set-env-vars="ANTHROPIC_VOICE_MODEL=claude-haiku-4-5" \
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
  --set-env-vars="GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/service-account.json" \
  --set-env-vars="LIVEKIT_URL=${LIVEKIT_URL}" \
  --set-env-vars="POSTHOG_API_KEY=phc_CDtz3DmNraHdnJ2w9W7WJNkJ8VANYPBWAcqV2Uf77k5s" \
  --set-env-vars="POSTHOG_HOST=https://us.i.posthog.com" \
  --set-env-vars="TELEGRAM_FEEDBACK_CHAT_ID=8599918865" \
  --set-secrets="TELEGRAM_BOT_TOKEN=juno-telegram-bot-token:latest" \
  --set-env-vars="DODO_API_BASE=${DODO_API_BASE}" \
  --set-env-vars="DODO_PRODUCT_COMPANION_MONTHLY=${DODO_PRODUCT_COMPANION_MONTHLY}" \
  --set-env-vars="DODO_PRODUCT_COMPANION_YEARLY=${DODO_PRODUCT_COMPANION_YEARLY}" \
  --set-env-vars="DODO_PRODUCT_PRO_MONTHLY=${DODO_PRODUCT_PRO_MONTHLY}" \
  --set-env-vars="DODO_PRODUCT_PRO_YEARLY=${DODO_PRODUCT_PRO_YEARLY}" \
  --set-env-vars="MEETINGS_AUDIO_BUCKET=juno-2ea45-meeting-audio"
  # ^ One-time prerequisites for the meetings bucket (NOT created by this
  # script): `gsutil mb -l us-central1 gs://juno-2ea45-meeting-audio` plus a
  # 7-day lifecycle DELETE rule. The lifecycle rule is a real privacy
  # backstop, not an optimization - the worker's post-synthesis audio delete
  # is best-effort, and without the rule a failed cleanup keeps raw meeting
  # audio indefinitely. Also enable the Firestore TTL policy:
  # `gcloud firestore fields ttls update expires_at --collection-group=meetings --enable-ttl`
  # After Dodo onboarding, create the two secrets (header above) and move these
  # two lines INTO the gcloud command block; referencing a secret that does not
  # exist yet fails the whole deploy, so they stay commented until then:
  #   --set-secrets="DODO_API_KEY=juno-dodo-api-key:latest" \
  #   --set-secrets="DODO_WEBHOOK_SECRET=juno-dodo-webhook-secret:latest" \

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

# Deletes a scheduler job that must no longer exist. Idempotent: a job already
# gone is success, so re-deploys stay clean.
remove_scheduler_job_if_exists() {
  local name="$1"
  if gcloud scheduler jobs describe "${name}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  • deleting retired job ${name}"
    gcloud scheduler jobs delete "${name}" --location="${REGION}" --project="${PROJECT_ID}" --quiet
  fi
}

ensure_scheduler_job "juno-reminder-tick" "* * * * *" "/scheduler/tick"
ensure_scheduler_job "juno-content-ingest" "0 */4 * * *" "/internal/signal-engine/content-ingest"

# Signal scoring is INGEST-TRIGGERED, not clock-triggered (2026-07-09): each
# completed content-ingest run enqueues one durable, generation-named Cloud Task
# that POSTs /internal/signal-engine/tick (see handlers/signal_content_ingest.py
# + services/signal_engine/generation_store.py). The old recurring scoring job
# re-ran the whole per-user KNN pipeline 16x per unchanged 4h pool, and a second
# cron here could race ingestion — so the retired job is actively deleted, never
# just left unreferenced.
remove_scheduler_job_if_exists "juno-signal-engine-tick"

# ── Prune old revisions + images (keep the 2 newest) ─────────────────────────
# Cloud Run keeps every past revision forever (they cost nothing to keep idle,
# but they pile up — this deploy inherited ~100). We keep exactly the 2 newest:
# the live one plus one instant-rollback target. The newest revision always
# carries 100% traffic (see --to-latest below), so it is never in the delete
# set. `|| true` on each delete keeps one still-referenced/undeletable revision
# from aborting the whole script (set -euo pipefail).
KEEP_REVISIONS=2
echo ""
echo "▶ Pruning Cloud Run revisions (keeping newest ${KEEP_REVISIONS})..."
gcloud run revisions list --service="${SERVICE_NAME}" --region="${REGION}" --project="${PROJECT_ID}" \
  --sort-by="~metadata.creationTimestamp" --format="value(metadata.name)" \
  | tail -n "+$((KEEP_REVISIONS + 1))" \
  | while read -r rev; do
      [[ -z "${rev}" ]] && continue
      echo "  • deleting old revision ${rev}"
      gcloud run revisions delete "${rev}" --region="${REGION}" --project="${PROJECT_ID}" --quiet || true
    done

# Untagged GCR image digests from prior builds. TWO things make a naive "delete
# every untagged digest" wrong here, both learned the hard way (2026-07-10):
#   1. The build produces a multi-arch manifest LIST (index) whose child
#      manifests (platform image + attestation) are themselves untagged. A child
#      cannot be deleted while its parent index still exists, so the registry
#      400s with "referenced by parent". Those errors are expected and harmless
#      — 2>/dev/null + `|| true` swallow them; the orphaned children delete on a
#      later run once their parent index is gone.
#   2. A surviving revision (the rollback target we deliberately kept above)
#      pins its image by digest. Deleting that digest would leave the rollback
#      revision unable to pull — silently breaking `update-traffic --to-revisions`.
#      So we skip every digest still referenced by a live revision.
echo "▶ Pruning untagged container images (keeping in-use + live-image digests)..."
IN_USE_DIGESTS="$(gcloud run revisions list --service="${SERVICE_NAME}" --region="${REGION}" \
  --project="${PROJECT_ID}" --format='value(status.imageDigest)' 2>/dev/null | sed 's/.*@//' | sort -u)"
gcloud container images list-tags "${IMAGE}" --filter="-tags:*" --format="get(digest)" \
  | while read -r digest; do
      [[ -z "${digest}" ]] && continue
      if [[ -n "${IN_USE_DIGESTS}" ]] && grep -qF "${digest}" <<<"${IN_USE_DIGESTS}"; then
        continue   # a kept revision still serves/rolls-back to this image
      fi
      gcloud container images delete "${IMAGE}@${digest}" --quiet 2>/dev/null || true
    done

echo "✅ Old revisions and images pruned"

# ── Voice worker ─────────────────────────────────────────────────────────────
# The voice worker NO LONGER runs on Cloud Run. It is hosted on LiveKit Cloud
# Agents (managed, pay-per-minute, scale-to-zero) to avoid paying for an
# always-on container. Deploy/update it from backend/ with:
#
#   lk agent deploy            # builds backend/Dockerfile (the worker image)
#
# Secrets/env live in LiveKit Cloud (lk agent update-secrets), not here. The
# Firebase service account is mounted via `lk agent ... --secret-mount
# ./service-account.json` at /etc/secrets/service-account.json.