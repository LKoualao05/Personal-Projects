#!/usr/bin/env bash
# Keyless continuous deployment: GitHub Actions -> Cloud Run via Workload Identity Federation.
# Only workflows on the main branch of this repository can impersonate the deployer.
#   ./deploy/setup_github_cd.sh            (needs gcloud + gh logged in)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${GCP_PROJECT:-$(python3 -c 'import json;print(json.load(open("config.json"))["GCP_PROJECT"])')}"
REGION="${REGION:-us-central1}"
REPO="${GITHUB_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
NUM="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
POOL=github
PROVIDER=github-oidc
DEPLOYER="gmail-tracker-deployer@${PROJECT}.iam.gserviceaccount.com"
RUN_SA="gmail-tracker-run@${PROJECT}.iam.gserviceaccount.com"
AR_REPO=gmail-tracker

gcloud config set project "${PROJECT}" >/dev/null

gcloud iam workload-identity-pools describe "${POOL}" --location=global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "${POOL}" --location=global --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers describe "${PROVIDER}" --location=global --workload-identity-pool="${POOL}" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" --location=global --workload-identity-pool="${POOL}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='${REPO}' && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts describe "${DEPLOYER}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create gmail-tracker-deployer --display-name="Gmail tracker (GitHub deployer)"
gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${AR_REPO}" --repository-format=docker --location="${REGION}"

gcloud projects add-iam-policy-binding "${PROJECT}" --member="serviceAccount:${DEPLOYER}" --role=roles/run.developer --condition=None --quiet >/dev/null
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" --location="${REGION}" \
  --member="serviceAccount:${DEPLOYER}" --role=roles/artifactregistry.writer --quiet >/dev/null
gcloud iam service-accounts add-iam-policy-binding "${RUN_SA}" --member="serviceAccount:${DEPLOYER}" \
  --role=roles/iam.serviceAccountUser --quiet >/dev/null
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER}" --role=roles/iam.workloadIdentityUser --quiet \
  --member="principalSet://iam.googleapis.com/projects/${NUM}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}" >/dev/null

gh variable set GCP_WIF_PROVIDER --repo "${REPO}" --body "projects/${NUM}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
gh variable set GCP_DEPLOYER_SA --repo "${REPO}" --body "${DEPLOYER}"
gh variable set GCP_PROJECT --repo "${REPO}" --body "${PROJECT}"
gh variable set GCP_REGION --repo "${REPO}" --body "${REGION}"
echo "Done: pushes to main now deploy automatically (.github/workflows/deploy.yml)."
