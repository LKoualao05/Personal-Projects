#!/usr/bin/env bash
# Build + deploy to Cloud Run (private, IAM-only) and schedule the daily sync.
#   ./deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${GCP_PROJECT:-$(python3 -c 'import json;print(json.load(open("config.json"))["GCP_PROJECT"])')}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-gmail-tracker}"
RUN_SA="gmail-tracker-run@${PROJECT}.iam.gserviceaccount.com"
SCHED_SA="gmail-tracker-scheduler@${PROJECT}.iam.gserviceaccount.com"
SCHEDULE="${SCHEDULE:-0 9 * * *}"   # 9 AM daily
TZ_NAME="${TZ_NAME:-America/New_York}"
SYNC_LIMIT="${SYNC_LIMIT:-500}"     # max Gemini calls per scheduled run

gcloud config set project "${PROJECT}" >/dev/null

echo "==> Deploying ${SERVICE} (Cloud Build from Dockerfile)"
gcloud run deploy "${SERVICE}" \
  --source . \
  --region "${REGION}" \
  --service-account "${RUN_SA}" \
  --no-allow-unauthenticated \
  --memory 1Gi --cpu 1 \
  --timeout 3600 \
  --concurrency 20 \
  --min-instances 0 --max-instances 1 \
  --set-env-vars "GCP_PROJECT=${PROJECT},GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=TRUE,TOKEN_PATH=/secrets/token.json" \
  --set-secrets "/secrets/token.json=gmail-oauth-token:latest" \
  --quiet

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo "==> Service URL: ${URL} (IAM-protected)"

echo "==> Cloud Scheduler: daily sync"
gcloud iam service-accounts describe "${SCHED_SA}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create gmail-tracker-scheduler --display-name="Gmail tracker (Cloud Scheduler invoker)"
gcloud run services add-iam-policy-binding "${SERVICE}" --region "${REGION}" \
  --member="serviceAccount:${SCHED_SA}" --role=roles/run.invoker --quiet >/dev/null
JOB_ARGS=(--location "${REGION}" --schedule "${SCHEDULE}" --time-zone "${TZ_NAME}"
  --uri "${URL}/tasks/sync?limit=${SYNC_LIMIT}" --http-method POST
  --oidc-service-account-email "${SCHED_SA}" --oidc-token-audience "${URL}" --attempt-deadline 1800s)
if gcloud scheduler jobs describe gmail-tracker-daily --location "${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http gmail-tracker-daily "${JOB_ARGS[@]}" --quiet >/dev/null
else
  # A just-created service account can take a minute to propagate (NOT_FOUND).
  for attempt in 1 2 3 4 5 6; do
    gcloud scheduler jobs create http gmail-tracker-daily "${JOB_ARGS[@]}" --quiet >/dev/null && break
    [ "${attempt}" = 6 ] && exit 1
    echo "   scheduler not ready yet (attempt ${attempt}); retrying in 20s"; sleep 20
  done
fi

cat <<EOF

Deployed. Try it:
  gcloud run services proxy ${SERVICE} --region ${REGION} --port 8080
  open http://localhost:8080/dev-ui/                     # ADK agent UI
  curl -X POST 'http://localhost:8080/tasks/sync?dry_run=true&limit=20'
  MCP endpoint for clients: http://localhost:8080/mcp/
  gcloud scheduler jobs run gmail-tracker-daily --location ${REGION}   # trigger the daily job now
EOF
