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
#   gcloud secrets create deepgram-dictation-api-key --project=<PROJECT_ID>
#   gcloud secrets create juno-groq-api-key --project=<PROJECT_ID>           # dictation polish: AI transcript cleanup
#   gcloud secrets create cartesia-api-key --project=<PROJECT_ID>
#   gcloud secrets create juno-google-client-id --project=<PROJECT_ID>
#   gcloud secrets create juno-google-client-secret --project=<PROJECT_ID>
#   gcloud secrets create juno-firebase-service-account --project=<PROJECT_ID>
#   gcloud secrets create juno-firebase-web-api-key --project=<PROJECT_ID>   # voice worker: mint ID tokens for /mcp
#   gcloud secrets create juno-openai-api-key --project=<PROJECT_ID>         # voice worker: primary voice LLM (gpt-4.1-mini)
#   gcloud secrets create juno-gemini-api-key --project=<PROJECT_ID>         # voice worker + signal engine LLM fallback
#   gcloud secrets create juno-brave-api-key --project=<PROJECT_ID>          # backend: real-time web_surf (chat + voice)
#   gcloud secrets create juno-newsdata-api-key --project=<PROJECT_ID>       # backend: signal-engine news pool (newsdata.io)
#   gcloud secrets create juno-firecrawl-api-key --project=<PROJECT_ID>      # backend: research page reading (services/research)
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
#   AND the ingest-triggered signal-scoring tasks. The juno-research queue
#   (settings.CLOUD_TASKS_RESEARCH_QUEUE) is provisioned and PINNED by this
#   script below: research stages must never inherit Cloud Tasks' defaults
#   (maxAttempts 100), because each redelivery spends provider credits on work
#   the engine already declared terminal.
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
MEETING_STORAGE_SERVICE_ACCOUNT="firebase-adminsdk-fbsvc@juno-2ea45.iam.gserviceaccount.com"
DICTATION_AUDIO_BUCKET="juno-2ea45-dictation-audio"
# Bounded pilot ceiling. A quick run has a separate $1.50 hard ceiling, so this
# permits at most three worst-case runs per UTC day and still fails closed.
PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD="5000000"

# Dodo Payments production catalog. The API key and webhook signing secret are
# injected from Secret Manager in the Cloud Run deploy block below.
DODO_API_BASE="https://live.dodopayments.com"
DODO_PRODUCT_COMPANION_MONTHLY="pdt_0NlKvXgFDbrqO4EC5Ads0"
DODO_PRODUCT_COMPANION_YEARLY="pdt_0NlKvfUD1Bic9YM9Olz4L"
DODO_PRODUCT_PRO_MONTHLY="pdt_0NlKvmk9Uzm0A4W7j3DNA"
DODO_PRODUCT_PRO_YEARLY="pdt_0NlKvvfC3x3KbqpGY9Hap"

echo "▶ Deploying ${SERVICE_NAME} to project=${PROJECT_ID} region=${REGION}"

# Enable required APIs (idempotent)
echo "▶ Enabling GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  containerregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudtasks.googleapis.com \
  --project="${PROJECT_ID}"

# GOOGLE_APPLICATION_CREDENTIALS points every Google client at the mounted Firebase
# service account, so that identity, not Cloud Run's runtime identity, creates tasks.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${MEETING_STORAGE_SERVICE_ACCOUNT}" \
  --role="roles/cloudtasks.enqueuer" \
  --condition=None \
  --quiet >/dev/null
gcloud iam service-accounts add-iam-policy-binding \
  "juno-scheduler@${PROJECT_ID}.iam.gserviceaccount.com" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${MEETING_STORAGE_SERVICE_ACCOUNT}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null

# Provision and PIN the research queue (idempotent). maxAttempts=2 matches
# store.STAGE_ATTEMPT_CAP; without this pin a new environment silently gets
# Cloud Tasks' defaults (maxAttempts 100, 500 dispatches/s) and every failed
# stage re-spends Brave/Firecrawl/model budget up to 100 times.
echo "▶ Provisioning juno-research Cloud Tasks queue..."
if ! gcloud tasks queues describe juno-research \
  --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud tasks queues create juno-research \
    --location="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null
fi
gcloud tasks queues update juno-research \
  --location="${REGION}" --project="${PROJECT_ID}" \
  --max-attempts=2 \
  --max-dispatches-per-second=10 \
  --max-concurrent-dispatches=20 \
  --min-backoff=10s \
  --max-backoff=300s \
  --quiet >/dev/null

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

# ── Meeting-audio storage preflight ──────────────────────────────────────────
# The new Cloud Run revision takes 100% of traffic the moment `gcloud run deploy`
# returns (there is no traffic-split stage below), so a missing/misconfigured
# meeting-audio bucket must be caught HERE, before traffic shifts. The 2026-07-14
# incident shipped a revision whose bucket was never provisioned: every segment
# upload 404'd, the handler answered 503, and no meeting ever produced a note.
# This read-only gate makes that unshippable. Under `set -euo pipefail` a non-zero
# exit aborts the deploy. Runtime upload failures stay retryable in the handler,
# so this gate does not change the at-runtime retry contract.
echo "▶ Preflighting meeting-audio bucket (existence, region, lifecycle, access)..."
# Both storage preflights run from backend/, the directory their docstrings assume,
# so any path relative to the backend root (notably the .env
# GOOGLE_APPLICATION_CREDENTIALS value) resolves the same way it does for every
# other backend command. The subshell keeps the cd out of the rest of the script
# and still aborts the deploy on a non-zero exit under `set -euo pipefail`.
(cd "${SCRIPT_DIR}" && python scripts/check_meeting_storage.py --check \
  --project "${PROJECT_ID}" \
  --bucket "juno-2ea45-meeting-audio" \
  --region "${REGION}" \
  --required-member "serviceAccount:${MEETING_STORAGE_SERVICE_ACCOUNT}" \
  --required-role "roles/storage.objectAdmin")

echo "▶ Preflighting dictation-audio bucket (region, prefix lifecycle, IAM)..."
(cd "${SCRIPT_DIR}" && python scripts/check_dictation_storage.py --check \
  --project "${PROJECT_ID}" \
  --bucket "${DICTATION_AUDIO_BUCKET}" \
  --region "${REGION}" \
  --required-member "serviceAccount:${MEETING_STORAGE_SERVICE_ACCOUNT}" \
  --required-role "roles/storage.objectAdmin")

# ── Firestore collection-group index preflight ───────────────────────────────
# Meeting reconciliation (services/meetings/operations.py, run hourly by
# /scheduler/tick at minute 27) filters the `meetings` COLLECTION GROUP on
# protocol_version. Firestore's default single-field config indexes every field
# at COLLECTION scope only, so that query additionally needs the explicit
# COLLECTION_GROUP field override in firestore.indexes.json — and needs it
# READY, not CREATING or absent.
#
# 2026-07-30: a revision carrying that query shipped before the override had
# ever been pushed to the default database. Every hourly tick logged
# meeting_reconciliation_failed ("400: query requires a collection-group
# index") for 35 hours across three revisions, and stopped mid-life of an
# unchanged revision the moment the index was finally created — the code was
# never the problem, the deploy order was.
#
# Pushing indexes stays a separate manual step (`firebase deploy --only
# firestore:indexes`) because an index build is not transactional with a
# traffic shift and can take arbitrarily long. So this gate does not create
# anything; it refuses to ship code whose required index is not already
# serving. A field override that does not exist at all makes `describe` fail
# and emit nothing, which lands on the same clear failure as a still-building
# one rather than a JSON traceback.
echo "▶ Preflighting Firestore collection-group indexes..."
read -r -d '' REQUIRE_READY_CG_INDEX_PY <<'PY' || true
import json
import sys

field = sys.argv[1]
raw = sys.stdin.read().strip()
config = (json.loads(raw) if raw else {}).get("indexConfig", {})
for index in config.get("indexes", []):
    paths = [entry.get("fieldPath") for entry in index.get("fields", [])]
    if (
        index.get("queryScope") == "COLLECTION_GROUP"
        and index.get("state") == "READY"
        and paths == [field]
    ):
        print(f"  • {field}: COLLECTION_GROUP index READY")
        break
else:
    states = [
        f'{i.get("queryScope")}/{i.get("state")}' for i in config.get("indexes", [])
    ]
    sys.exit(
        f"  x {field}: no READY COLLECTION_GROUP index (found: {states or 'none'}).\n"
        "    Run `firebase deploy --only firestore:indexes`, wait for the build to\n"
        "    reach READY, then re-run this deploy."
    )
PY
gcloud firestore indexes fields describe protocol_version \
  --collection-group=meetings --project="${PROJECT_ID}" --format=json 2>/dev/null \
  | python -c "${REQUIRE_READY_CG_INDEX_PY}" protocol_version \
  || { echo "  x meetings.protocol_version index preflight failed (see above)"; exit 1; }
gcloud firestore indexes fields describe audio_expires_at \
  --collection-group=dictation_traces --project="${PROJECT_ID}" --format=json 2>/dev/null \
  | python -c "${REQUIRE_READY_CG_INDEX_PY}" audio_expires_at \
  || { echo "  x dictation_traces.audio_expires_at index preflight failed"; exit 1; }

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
  --set-env-vars="PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD=${PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD}" \
  --set-env-vars="REALTIME_BRIDGE_ENABLED=false" \
  --set-env-vars="ANTHROPIC_CHAT_MODEL=claude-sonnet-4-6" \
  --set-env-vars="ANTHROPIC_VOICE_MODEL=claude-haiku-4-5" \
  --set-env-vars="ANTHROPIC_MAX_TOKENS=8096" \
  --set-env-vars="GOOGLE_REDIRECT_URI=${STABLE_SERVICE_URL}/connectors/oauth/google/callback" \
  --set-env-vars="NOTION_REDIRECT_URI=${STABLE_SERVICE_URL}/connectors/oauth/notion/callback" \
  --set-env-vars="BACKEND_INTERNAL_URL=${STABLE_SERVICE_URL}" \
  --set-env-vars="SCHEDULER_OIDC_AUDIENCES=${ACCEPTED_AUDIENCES}" \
  --set-secrets="ANTHROPIC_API_KEY=juno-anthropic-api-key:latest" \
  --set-secrets="OPENAI_API_KEY=juno-openai-api-key:latest" \
  --set-secrets="LIVEKIT_API_KEY=livekit-api-key:latest" \
  --set-secrets="LIVEKIT_API_SECRET=livekit-api-secret:latest" \
  --set-secrets="DEEPGRAM_API_KEY=deepgram-api-key:latest" \
  --set-secrets="DEEPGRAM_DICTATION_API_KEY=deepgram-dictation-api-key:latest" \
  --set-secrets="GROQ_API_KEY=juno-groq-api-key:latest" \
  --set-secrets="CARTESIA_API_KEY=cartesia-api-key:latest" \
  --set-secrets="GOOGLE_CLIENT_ID=juno-google-client-id:latest" \
  --set-secrets="GOOGLE_CLIENT_SECRET=juno-google-client-secret:latest" \
  --set-secrets="NOTION_CLIENT_ID=juno-notion-client-id:latest" \
  --set-secrets="NOTION_CLIENT_SECRET=juno-notion-client-secret:latest" \
  --set-secrets="GEMINI_API_KEY=juno-gemini-api-key:latest" \
  --set-secrets="BRAVE_API_KEY=juno-brave-api-key:latest" \
  --set-secrets="NEWSDATA_API_KEY=juno-newsdata-api-key:latest" \
  --set-secrets="FIRECRAWL_API_KEY=juno-firecrawl-api-key:latest" \
  --set-secrets="DODO_API_KEY=juno-dodo-api-key:latest" \
  --set-secrets="DODO_WEBHOOK_SECRET=juno-dodo-webhook-secret:latest" \
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
  --set-env-vars="MEETINGS_AUDIO_BUCKET=juno-2ea45-meeting-audio" \
  --set-env-vars="DICTATION_AUDIO_BUCKET=${DICTATION_AUDIO_BUCKET}"
  # ^ One-time prerequisites for the meetings bucket (NOT created by this
  # script, but now VERIFIED by the storage preflight above before every
  # deploy - a missing bucket or absent lifecycle rule fails the deploy).
  # Provision once (gcloud storage; gsutil still works but Google is retiring it):
  #   gcloud storage buckets create gs://juno-2ea45-meeting-audio \
  #     --location=us-central1 --uniform-bucket-level-access
  #   gcloud storage buckets update gs://juno-2ea45-meeting-audio \
  #     --lifecycle-file=scripts/lifecycle-7day-delete.json
  # RE-APPLY that rule if the bucket still carries the original hand-applied one.
  # This file did not exist in the repo until now, so the live rule was created by
  # hand and was bucket-WIDE by age. That also deleted transcripts/v2/**, which is
  # where canonical/webvtt/quality-report/note-input live - and a Pro meeting has
  # no Firestore TTL, so its "ready" note kept pointing at artifacts that had been
  # deleted on day 7. The committed rule is scoped to the source-audio prefixes
  # only. check_meeting_storage.py matches on delete AGE, not prefix, so the
  # scoped rule still satisfies the deploy gate.
  #   gcloud storage buckets add-iam-policy-binding gs://juno-2ea45-meeting-audio \
  #     --member=serviceAccount:firebase-adminsdk-fbsvc@juno-2ea45.iam.gserviceaccount.com \
  #     --role=roles/storage.objectAdmin
  # Dictation training audio is deliberately isolated from meeting retention.
  # Provisioned 2026-08-06; run from backend/ so the lifecycle path resolves:
  #   gcloud storage buckets create gs://juno-2ea45-dictation-audio \
  #     --location=us-central1 --uniform-bucket-level-access \
  #     --public-access-prevention --soft-delete-duration=0
  #   gcloud storage buckets update gs://juno-2ea45-dictation-audio \
  #     --lifecycle-file=scripts/lifecycle-dictation-180day-delete.json
  #   gcloud storage buckets add-iam-policy-binding gs://juno-2ea45-dictation-audio \
  #     --member=serviceAccount:firebase-adminsdk-fbsvc@juno-2ea45.iam.gserviceaccount.com \
  #     --role=roles/storage.objectAdmin
  #   gcloud iam roles create dictationBucketPreflight --project=juno-2ea45 \
  #     --title="Dictation Bucket Preflight" --stage=GA \
  #     --permissions=storage.buckets.get,storage.buckets.getIamPolicy
  #   gcloud storage buckets add-iam-policy-binding gs://juno-2ea45-dictation-audio \
  #     --member=serviceAccount:firebase-adminsdk-fbsvc@juno-2ea45.iam.gserviceaccount.com \
  #     --role=projects/juno-2ea45/roles/dictationBucketPreflight
  # That last binding is NOT optional and is easy to miss: objectAdmin carries no
  # storage.buckets.getIamPolicy, so check_dictation_storage.py --check dies with a
  # 403 traceback (not a clean [FAIL]) and aborts the whole deploy under `set -e`.
  # juno-2ea45-meeting-audio only passes its own preflight because it additionally
  # holds roles/storage.admin, which this recipe never recorded. The custom role is
  # the least-privilege equivalent: bucket metadata + IAM read, nothing else.
  # soft-delete is 0 here (meeting-audio keeps the 7-day GCS default) so that
  # DELETE /dictation/traces/{id} and account deletion drop the bytes immediately
  # rather than leaving them admin-recoverable for a week.
  # The IAM grant targets the FIREBASE service account, because the backend runs
  # every Google client (incl. Cloud Storage) as GOOGLE_APPLICATION_CREDENTIALS
  # (=/run/secrets/service-account.json, the juno-firebase-service-account secret),
  # not the Cloud Run runtime SA. The 7-day lifecycle DELETE rule is the configured
  # retention policy, not worker cleanup: successful V2 transcription/publication
  # deliberately retains source audio, and explicit user deletion uses exact object
  # generations. Also
  # enable the Firestore TTL policy (not covered by the preflight):
  # `gcloud firestore fields ttls update expires_at --collection-group=meetings --enable-ttl`
# Cloud Run preserves an explicit revision/tag traffic assignment across future
# deploys. A tagged hotfix once left the stable production URL pinned to the old
# revision while every later `gcloud run deploy` created a healthy revision at
# 0% traffic. Restore the LATEST allocation explicitly on every production deploy
# so this deploy and future deploys actually reach users.
DEPLOYED_REVISION="$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.latestCreatedRevisionName)")"
if [[ -z "${DEPLOYED_REVISION}" ]]; then
  echo "Cloud Run deploy verification failed: no latest created revision" >&2
  exit 1
fi
echo "▶ Promoting the latest Cloud Run revision to 100% traffic..."
gcloud run services update-traffic "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --to-latest

# Do not print "deployed" unless the control plane confirms that the stable URL
# follows LATEST at 100%. This checks the allocation type, not just whichever
# revision happens to be serving today, so a later deploy cannot silently fall
# back to a stale explicit revision.
DEPLOYED_SERVICE_STATE="$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format=json)"
SERVING_REVISION="$(python -c '
import json
import sys

service = json.load(sys.stdin)
expected = sys.argv[1]
traffic = service.get("status", {}).get("traffic", [])
latest = next(
    (
        target
        for target in traffic
        if target.get("latestRevision") is True and target.get("percent") == 100
    ),
    None,
)
if latest is None or latest.get("revisionName") != expected:
    actual = latest.get("revisionName") if latest else "none"
    sys.exit(
        f"Cloud Run traffic verification failed: deployed={expected}, serving={actual}"
    )
print(latest["revisionName"])
' "${DEPLOYED_REVISION}" <<<"${DEPLOYED_SERVICE_STATE}")"
echo "Cloud Run traffic verified: LATEST=100% (${SERVING_REVISION})"

# Print service URL
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")

DEPLOYED_RESEARCH_COST_CAP="$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --format=json | python -c 'import json, sys; data = json.load(sys.stdin); env = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("env", []); print(next((str(item.get("value", "")) for item in env if item.get("name") == "PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD"), ""))')"
if [[ "${DEPLOYED_RESEARCH_COST_CAP}" != "${PROJECT_RESEARCH_DAILY_COST_CAP_MICROUSD}" ]]; then
  echo "Research cost kill switch read-back failed" >&2
  exit 1
fi
echo "Research cost kill switch verified at ${DEPLOYED_RESEARCH_COST_CAP} micro-USD"

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
# but they pile up — this deploy inherited ~100). Keep the 2 newest plus every
# revision still referenced by a traffic target or tag. A tagged revision can be
# referenced at 0%, and Cloud Run still refuses to delete it. `|| true` keeps an
# unexpected control-plane race from aborting an otherwise successful deploy.
KEEP_REVISIONS=2
echo ""
echo "▶ Pruning Cloud Run revisions (keeping newest ${KEEP_REVISIONS})..."
ACTIVE_REVISIONS="$(python -c '
import json
import sys

service = json.load(sys.stdin)
names = sorted({
    target.get("revisionName", "")
    for target in service.get("status", {}).get("traffic", [])
    if target.get("revisionName")
})
print("\n".join(names))
' <<<"${DEPLOYED_SERVICE_STATE}")"
gcloud run revisions list --service="${SERVICE_NAME}" --region="${REGION}" --project="${PROJECT_ID}" \
  --sort-by="~metadata.creationTimestamp" --format="value(metadata.name)" \
  | tr -d '\r' \
  | tail -n "+$((KEEP_REVISIONS + 1))" \
  | while read -r rev; do
      [[ -z "${rev}" ]] && continue
      if grep -qxF "${rev}" <<<"${ACTIVE_REVISIONS}"; then
        echo "  • keeping referenced revision ${rev}"
        continue
      fi
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
  --project="${PROJECT_ID}" --format='value(status.imageDigest)' 2>/dev/null | tr -d '\r' | sed 's/.*@//' | sort -u)"
gcloud container images list-tags "${IMAGE}" --filter="-tags:*" --format="get(digest)" \
  | tr -d '\r' \
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
