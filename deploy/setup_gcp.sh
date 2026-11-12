#!/usr/bin/env bash
# One-time (idempotent) Google Cloud setup for the tracker.
#   GCP_PROJECT=my-project REGION=us-central1 ./deploy/setup_gcp.sh
# Requires: gcloud logged in as a project owner, billing enabled, token.json from `python main.py --reauth`.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${GCP_PROJECT:-$(python3 -c 'import json;print(json.load(open("config.json"))["GCP_PROJECT"])')}"
REGION="${REGION:-us-central1}"
RUN_SA="gmail-tracker-run@${PROJECT}.iam.gserviceaccount.com"
SECRET="gmail-oauth-token"
EMBED_DIMS="${EMBEDDING_DIMS:-768}"

echo "==> Project ${PROJECT} (${REGION})"
gcloud config set project "${PROJECT}" >/dev/null

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  cloudscheduler.googleapis.com gmail.googleapis.com sheets.googleapis.com iamcredentials.googleapis.com

echo "==> Runtime service account (least privilege)"
gcloud iam service-accounts describe "${RUN_SA}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create gmail-tracker-run --display-name="Gmail tracker (Cloud Run runtime)"
for role in roles/aiplatform.user roles/datastore.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT}" --member="serviceAccount:${RUN_SA}" --role="${role}" \
    --condition=None --quiet >/dev/null
done

echo "==> Firestore (native mode) + vector index"
gcloud firestore databases describe --database="(default)" >/dev/null 2>&1 || \
  gcloud firestore databases create --location=nam5 --type=firestore-native
gcloud firestore indexes composite create --collection-group=job_description_chunks --query-scope=COLLECTION \
  --field-config="field-path=embedding,vector-config={\"dimension\":\"${EMBED_DIMS}\",\"flat\":\"{}\"}" --async 2>&1 \
  | grep -v "already exists" || true

echo "==> Gmail/Sheets OAuth token -> Secret Manager"
test -f token.json || { echo "token.json missing: run 'python main.py --reauth' first"; exit 1; }
gcloud secrets describe "${SECRET}" >/dev/null 2>&1 || \
  gcloud secrets create "${SECRET}" --replication-policy=automatic
gcloud secrets versions add "${SECRET}" --data-file=token.json >/dev/null
gcloud secrets add-iam-policy-binding "${SECRET}" --member="serviceAccount:${RUN_SA}" \
  --role=roles/secretmanager.secretAccessor --quiet >/dev/null

echo "Done. Next: ./deploy/deploy.sh"
