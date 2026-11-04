#!/bin/bash
# One-time GCP setup for the Cloud Run deploy in .github/workflows/deploy.yml.
#
# Run it once per project, by someone with Owner (or IAM Admin + Service Usage
# Admin) on the project. It is safe to run again: every step skips what exists.
#
#   PROJECT_ID=ai-agent-boilerplate0 REGION=us-central1 \
#   DEPLOY_SA=github-deployer@ai-agent-boilerplate0.iam.gserviceaccount.com \
#   ./scripts/gcp_setup.sh
#
# DEPLOY_SA is the service account whose key is in the GCP_SA_KEY GitHub
# secret. It gets the roles the workflow needs; nothing else is changed on it.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
DEPLOY_SA="${DEPLOY_SA:?set DEPLOY_SA to the GitHub deploy service account email}"
AR_REPOSITORY="zero-sales-agent"
RUNTIME_SA="zero-sales-agent-run@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="zero-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
SECRETS=(GROQ_API_KEY POSTGRES_URL SLACK_WEBHOOK_URL)

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com

echo "== Artifact Registry repository ${AR_REPOSITORY} (${REGION})"
gcloud artifacts repositories describe "$AR_REPOSITORY" --location "$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$AR_REPOSITORY" \
       --location "$REGION" --repository-format docker \
       --description "Zer0 sales agent images"

# Keep the image store small: delete images older than 30 days, but always
# keep the 20 newest so a rollback target is there.
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<'EOF'
[
  {"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 20}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "30d"}}
]
EOF
gcloud artifacts repositories set-cleanup-policies "$AR_REPOSITORY" \
  --location "$REGION" --policy "$POLICY_FILE" >/dev/null

create_sa() {
  local email="$1" display="$2"
  local name="${email%%@*}"
  gcloud iam service-accounts describe "$email" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$name" --display-name "$display"
}

echo "== Service accounts"
create_sa "$RUNTIME_SA" "Zer0 sales agent (Cloud Run runtime)"
create_sa "$SCHEDULER_SA" "Zer0 sales agent (Cloud Scheduler invoker)"

echo "== Runtime account reads the app secrets"
for secret in "${SECRETS[@]}"; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role roles/secretmanager.secretAccessor >/dev/null
done

echo "== Deploy account (${DEPLOY_SA}) can build, deploy and schedule"
for role in roles/run.admin roles/artifactregistry.writer roles/cloudscheduler.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${DEPLOY_SA}" --role "$role" --condition None >/dev/null
done
# Deploying a service as RUNTIME_SA, and a scheduler job that signs tokens as
# SCHEDULER_SA, both need "act as" on that account.
for sa in "$RUNTIME_SA" "$SCHEDULER_SA"; do
  gcloud iam service-accounts add-iam-policy-binding "$sa" \
    --member "serviceAccount:${DEPLOY_SA}" \
    --role roles/iam.serviceAccountUser >/dev/null
done

cat <<EOF

Done. Next:
  1. ./scripts/lb_cutover.sh prepare  (load balancer backends, no traffic)
  2. Merge to staging, then ./scripts/lb_cutover.sh switch staging.
  3. CLOUD_SCHEDULER_ENABLED=true only once the Zer0 app on the VMs is
     stopped (until then the summary job stays paused; see DEPLOYMENT.md).
EOF
