#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-openclaw-mc}"
REGION="${REGION:-europe-west1}"
SERVICE_NAME="${SERVICE_NAME:-clawmetry-cloud}"

echo "Deploying ${SERVICE_NAME} to Cloud Run"
echo "Project: ${PROJECT_ID} | Region: ${REGION}"
echo ""

if ! command -v gcloud >/dev/null 2>&1; then
  echo "ERROR: gcloud not found" >&2
  exit 1
fi

# Deploy from repo root (uses ./Dockerfile + ./.dockerignore)
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 10 \
  --timeout 300 \
  --set-env-vars "PYTHONUNBUFFERED=1"

# Make publicly accessible (org-policy friendly)
gcloud run services update "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --platform managed \
  --no-invoker-iam-check

URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format='value(status.url)')"

echo ""
echo "Deploy complete."
echo "Service URL: ${URL}"
echo ""
echo "Next: add TURSO_URL + TURSO_TOKEN env vars:"
echo "  gcloud run services update ${SERVICE_NAME} \\"
echo "    --project ${PROJECT_ID} --region ${REGION} \\"
echo "    --set-env-vars 'TURSO_URL=...,TURSO_TOKEN=...'"
