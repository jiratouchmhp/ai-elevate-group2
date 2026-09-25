#!/usr/bin/env bash
# One-command deploy for the Altostrat HR agent MVP (SDD §7). Every step is idempotent.
#
#   deployment/deploy.sh all            # bootstrap -> infra -> secrets -> rag -> build -> apply -> smoke
#   deployment/deploy.sh <step>         # bootstrap | infra | push-secrets | rag-sync | build | apply | smoke | plan
#
# Reads project/region from deployment/terraform/terraform.tfvars and the vendor PAT from
# the local, gitignored .env (BACKEND_SHARED_TOKEN / BACKEND_TOKENS_JSON). The PAT goes
# straight to Secret Manager — never into Terraform variables or state.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/deployment/terraform"
TFVARS="$TF_DIR/terraform.tfvars"
cd "$ROOT"

die() { echo "error: $*" >&2; exit 1; }
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

[[ -f "$TFVARS" ]] || die "missing $TFVARS — cp terraform.tfvars.example terraform.tfvars and fill it in"
tfvar() { sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$TFVARS" | head -1; }
set_tfvar() {  # set_tfvar key value — insert or replace a string var in terraform.tfvars
  local k="$1" v="$2"
  if grep -qE "^[[:space:]]*$k[[:space:]]*=" "$TFVARS"; then
    sed -i.bak -E "s|^[[:space:]]*$k[[:space:]]*=.*|$k = \"$v\"|" "$TFVARS" && rm -f "$TFVARS.bak"
  else
    printf '%s = "%s"\n' "$k" "$v" >> "$TFVARS"
  fi
}
dotenv() { [[ -f "$ROOT/.env" ]] && sed -nE "s/^$1=(.*)$/\1/p" "$ROOT/.env" | tail -1 | sed -E 's/^"(.*)"$/\1/'; }

PROJECT="$(tfvar project_id)"; [[ -n "$PROJECT" ]] || die "project_id not set in terraform.tfvars"
REGION="$(tfvar region)"; REGION="${REGION:-asia-southeast1}"
STATE_BUCKET="${PROJECT}-hr-agent-tfstate"
if [[ -z "${TF_VAR_deployer_member:-}" ]]; then
  _acct="$(gcloud config get account 2>/dev/null || true)"
  if [[ "$_acct" == *gserviceaccount.com ]]; then export TF_VAR_deployer_member="serviceAccount:$_acct"
  elif [[ -n "$_acct" ]]; then export TF_VAR_deployer_member="user:$_acct"; fi
fi
TAG="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
[[ -z "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]] || TAG="${TAG}-$(date +%H%M%S)"

tf() { terraform -chdir="$TF_DIR" "$@"; }
tfout() { tf output -raw "$1"; }

step_bootstrap() {
  log "bootstrap: tfstate bucket gs://$STATE_BUCKET + terraform init"
  command -v terraform >/dev/null || die "terraform not installed (brew install hashicorp/tap/terraform)"
  gcloud services enable storage.googleapis.com cloudresourcemanager.googleapis.com --project "$PROJECT"
  if ! gcloud storage buckets describe "gs://$STATE_BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://$STATE_BUCKET" --project "$PROJECT" --location "$REGION" \
      --uniform-bucket-level-access --public-access-prevention
    gcloud storage buckets update "gs://$STATE_BUCKET" --versioning
  fi
  tf init -input=false -reconfigure -backend-config="bucket=$STATE_BUCKET"
}

step_infra() {
  log "infra: APIs, registry, identities, secrets, buckets, Firestore, Model Armor, audit sink"
  tf apply -input=false -auto-approve \
    -target=google_project_service.apis \
    -target=google_artifact_registry_repository.hr \
    -target=google_artifact_registry_repository_iam_member.build_writer \
    -target=google_project_iam_member.build \
    -target=google_service_account_iam_member.deployer_acts_as_build \
    -target=google_storage_bucket_iam_member.build_staging \
    -target=google_secret_manager_secret.vendor_pat \
    -target=google_secret_manager_secret.vendor_tokens \
    -target=google_secret_manager_secret_version.envelope \
    -target=google_secret_manager_secret_version.confirm \
    -target=google_storage_bucket.corpus \
    -target=google_storage_bucket.artifacts \
    -target=google_firestore_database.ledger \
    -target=google_model_armor_template.input \
    -target=google_model_armor_template.output \
    -target=google_project_iam_member.model_armor_dlp \
    -target=google_bigquery_dataset_iam_member.sink_writer
}

step_push_secrets() {
  log "push-secrets: vendor PAT(s) from .env -> Secret Manager"
  local pat tokens
  pat="$(dotenv BACKEND_SHARED_TOKEN || true)"
  tokens="$(dotenv BACKEND_TOKENS_JSON || true)"
  [[ -n "$pat$tokens" ]] || die "neither BACKEND_SHARED_TOKEN nor BACKEND_TOKENS_JSON is set in .env"
  if [[ -n "$pat" ]]; then
    printf '%s' "$pat" | gcloud secrets versions add workweek-si-pat --project "$PROJECT" --data-file=- >/dev/null
    echo "workweek-si-pat: new version added"
  fi
  if [[ -n "$tokens" ]]; then
    gcloud secrets describe hr-backend-tokens --project "$PROJECT" >/dev/null 2>&1 \
      || die "BACKEND_TOKENS_JSON set but hr-backend-tokens secret missing — set enable_per_persona_tokens = true"
    printf '%s' "$tokens" | gcloud secrets versions add hr-backend-tokens --project "$PROJECT" --data-file=- >/dev/null
    echo "hr-backend-tokens: new version added"
  fi
}

step_rag_sync() {
  log "rag-sync: governed chunks -> Vertex AI RAG Engine ($REGION)"
  GOOGLE_CLOUD_PROJECT="$PROJECT" uv run python -m app.policy.rag_sync \
    --project "$PROJECT" --location "$REGION" --bucket "$(tfout corpus_bucket)" --write-tfvars "$TFVARS"
}

step_build() {
  log "build: Cloud Build -> $(tfout artifact_registry) (tag $TAG)"
  local registry; registry="$(tfout artifact_registry)"
  gcloud builds submit "$ROOT" --project "$PROJECT" --region "$REGION" \
    --config "$ROOT/deployment/cloudbuild.yaml" \
    --service-account "projects/$PROJECT/serviceAccounts/$(tfout build_service_account)" \
    --gcs-source-staging-dir "gs://$(tfout build_staging_bucket)/source" \
    --substitutions "_REGISTRY=$registry,_TAG=$TAG"
  set_tfvar agent_image "$registry/hr-agent:$TAG"
  set_tfvar acl_image "$registry/hr-acl:$TAG"
  set_tfvar agent_version "$TAG"
}

step_plan() { tf plan -input=false; }

step_apply() {
  log "apply: full stack (Cloud Run services, IAM, IAP, logging)"
  tf apply -input=false -auto-approve
  echo; tf output
}

step_smoke() {
  log "smoke: health + access controls"
  local acl agent code
  acl="$(tfout acl_url)"; agent="$(tfout agent_url)"
  code="$(curl -s -o /dev/null -w '%{http_code}' "$acl/health")"
  [[ "$code" == "403" || "$code" == "401" ]] && echo "ok  hr-acl rejects unauthenticated callers ($code)" \
    || echo "WARN hr-acl returned $code to an unauthenticated call (expected 401/403)"
  code="$(curl -s -o /dev/null -w '%{http_code}' "$agent/")"
  [[ "$code" == "302" || "$code" == "401" || "$code" == "403" ]] && echo "ok  hr-agent is behind IAP ($code)" \
    || echo "WARN hr-agent returned $code without IAP (expected 302/401/403)"
  # /health, not /healthz: run.app's front end reserves paths ending in "z".
  echo "hr-acl /health (impersonating hr-agent-sa):"
  local tok
  tok="$(gcloud auth print-identity-token --impersonate-service-account="$(tfout agent_service_account)" \
      --audiences="$acl" 2>/dev/null || true)"
  if [[ -n "$tok" ]]; then
    curl -s -H "Authorization: Bearer $tok" "$acl/health"
  else
    echo "skipped: needs roles/iam.serviceAccountTokenCreator on hr-agent-sa (revision readiness already proves /healthz)"
  fi
  echo; echo "Chat UI: $agent  (sign in with an iap_members account)"
}

case "${1:-all}" in
  bootstrap) step_bootstrap ;;
  infra) step_infra ;;
  push-secrets) step_push_secrets ;;
  rag-sync) step_rag_sync ;;
  build) step_build ;;
  plan) step_plan ;;
  apply) step_apply ;;
  smoke) step_smoke ;;
  all) step_bootstrap; step_infra; step_push_secrets; step_rag_sync; step_build; step_apply; step_smoke ;;
  *) die "unknown step '$1'" ;;
esac
