#!/usr/bin/env bash
# Reference deployment. Read it before running; every value is a placeholder.
# Auth: `gcloud auth login` + `gcloud auth application-default login`. No service account keys.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-YOUR_PROJECT_ID}"
REGION="${REGION:-YOUR_REGION}"
SA="oncall-agent@${PROJECT_ID}.iam.gserviceaccount.com"
TAG="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/oncall/gcp-oncall-agent:${TAG}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ "$PROJECT_ID" == "YOUR_PROJECT_ID" || "$REGION" == "YOUR_REGION" ]]; then
  echo "Set PROJECT_ID and REGION first." >&2
  exit 1
fi

gcloud services enable run.googleapis.com logging.googleapis.com monitoring.googleapis.com \
  cloudbuild.googleapis.com container.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com aiplatform.googleapis.com --project "$PROJECT_ID"

# 1. Identity + least-privilege custom role
gcloud iam service-accounts create oncall-agent --project "$PROJECT_ID" \
  --display-name "gcp-oncall-agent runtime" || true
gcloud iam roles create oncallAgentReader --project "$PROJECT_ID" \
  --file "${HERE}/iam/oncall-reader-role.yaml" || true
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role "projects/${PROJECT_ID}/roles/oncallAgentReader"
# Vertex AI for the LLM
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role "roles/aiplatform.user"

# 2. Secrets: values are typed interactively and never written to disk or the repo
for s in oncall-webhook-token oncall-approval-secret; do
  gcloud secrets create "$s" --project "$PROJECT_ID" --replication-policy automatic || true
  echo "Enter value for $s (input hidden):"
  read -rs value
  printf '%s' "$value" | gcloud secrets versions add "$s" --project "$PROJECT_ID" --data-file=-
  unset value
  gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT_ID" \
    --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor
done

# 3. Build and deploy
gcloud artifacts repositories create oncall --project "$PROJECT_ID" --location "$REGION" \
  --repository-format docker || true
gcloud builds submit --project "$PROJECT_ID" --tag "$IMAGE" .
sed -e "s#YOUR_PROJECT_ID#${PROJECT_ID}#g" -e "s#YOUR_REGION#${REGION}#g" \
  -e "s#gcp-oncall-agent:latest#gcp-oncall-agent:${TAG}#" "${HERE}/service.yaml" > "${TMPDIR:-/tmp}/oncall-service.yaml"
gcloud run services replace "${TMPDIR:-/tmp}/oncall-service.yaml" --project "$PROJECT_ID" --region "$REGION"

echo "Next: create a Cloud Monitoring webhook or Pub/Sub notification channel pointing at the"
echo "service (see deploy/cloudrun/README.md) and put the UI behind IAP."
