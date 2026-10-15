#!/usr/bin/env bash
# Deploy Gemini Podcasts to Cloud Run in a Google Cloud project.
#
#   FIREBASE_API_KEY=... ADMIN_EMAILS=you@example.com deploy/deploy.sh PROJECT_ID [REGION]
#
# Before the first run, add Firebase to the project and turn on sign-in (see
# "Deploy to Google Cloud" in the README): that gives you FIREBASE_API_KEY.
#
# Run it from Git Bash, macOS/Linux, or Cloud Shell. Needs gcloud (logged in
# with `gcloud auth login`), docker, and openssl. Safe to re-run: every step
# checks before it creates, and each run deploys a fresh image.
#
# Settings, from the environment:
#   FIREBASE_API_KEY       required: the Firebase web app's API key (public, not a secret)
#   ADMIN_EMAILS           required: comma-separated; they can grant credits
#   FIREBASE_AUTH_DOMAIN   default PROJECT_ID.firebaseapp.com
#   SIGNUP_CREDITS         default 0 (new accounts are free accounts)
#   GEMINI_API_KEY         asked for on the first run if not set; kept in Secret Manager
# To change the Gemini key later:
#   printf '%s' 'new key' | gcloud secrets versions add gemini-api-key --data-file=- --project PROJECT_ID
# then re-run this script (Cloud Run reads :latest at deploy time).
set -euo pipefail

PROJECT_ID="${1:?usage: FIREBASE_API_KEY=... ADMIN_EMAILS=... deploy/deploy.sh PROJECT_ID [REGION]}"
REGION="${2:-us-central1}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-$REGION}"
FIREBASE_API_KEY="${FIREBASE_API_KEY:?Set FIREBASE_API_KEY (Firebase console > Project settings > Your apps > Web app)}"
ADMIN_EMAILS="${ADMIN_EMAILS:?Set ADMIN_EMAILS, e.g. you@example.com}"
FIREBASE_AUTH_DOMAIN="${FIREBASE_AUTH_DOMAIN:-${PROJECT_ID}.firebaseapp.com}"
SIGNUP_CREDITS="${SIGNUP_CREDITS:-0}"
SERVICE="podcasts"
REPO="podcasts"
BUCKET="${PROJECT_ID}-podcasts"
RUN_SA_NAME="podcasts-run"
RUN_SA="${RUN_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(date +%Y%m%d-%H%M%S)"

cd "$(dirname "$0")/.."

say() { printf '\n==> %s\n' "$*"; }
g() { gcloud --project "$PROJECT_ID" --quiet "$@"; }

say "Checking project $PROJECT_ID"
g projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null
billing="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
if [[ "$billing" != "True" ]]; then
  echo "Billing isn't enabled on $PROJECT_ID. Link a billing account first:"
  echo "  https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT_ID"
  exit 1
fi

say "Enabling APIs (a minute or two the first time)"
g services enable \
  run.googleapis.com artifactregistry.googleapis.com firestore.googleapis.com \
  storage.googleapis.com secretmanager.googleapis.com iamcredentials.googleapis.com \
  identitytoolkit.googleapis.com

say "Artifact Registry repository: $REPO"
g artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
  g artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
    --description "Gemini Podcasts images"

say "Firestore database (native mode, $FIRESTORE_LOCATION)"
g firestore databases describe --database='(default)' >/dev/null 2>&1 || \
  g firestore databases create --database='(default)' --location "$FIRESTORE_LOCATION" --type firestore-native

say "Cloud Storage bucket: gs://$BUCKET"
gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT_ID" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention

say "Runtime service account: $RUN_SA"
g iam service-accounts describe "$RUN_SA" >/dev/null 2>&1 || \
  g iam service-accounts create "$RUN_SA_NAME" --display-name "Gemini Podcasts (Cloud Run)"

say "Secrets"
create_secret() {  # name value
  printf '%s' "$2" | g secrets create "$1" --replication-policy automatic --data-file=- >/dev/null
  echo "  created $1"
}
if g secrets describe gemini-api-key >/dev/null 2>&1; then
  echo "  gemini-api-key exists, keeping it"
else
  key="${GEMINI_API_KEY:-}"
  if [[ -z "$key" ]]; then read -rsp "  Gemini API key (https://aistudio.google.com/apikey): " key; echo; fi
  [[ -n "$key" ]] || { echo "A Gemini API key is required."; exit 1; }
  create_secret gemini-api-key "$key"
fi
if g secrets describe session-secret >/dev/null 2>&1; then
  echo "  session-secret exists, keeping it"
else
  create_secret session-secret "$(openssl rand -base64 48 | tr -d '\n')"
fi

say "Permissions for $RUN_SA"
g projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$RUN_SA" \
  --role roles/datastore.user --condition=None >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --project "$PROJECT_ID" \
  --member "serviceAccount:$RUN_SA" --role roles/storage.objectAdmin >/dev/null
for secret in gemini-api-key session-secret; do
  g secrets add-iam-policy-binding "$secret" --member "serviceAccount:$RUN_SA" \
    --role roles/secretmanager.secretAccessor >/dev/null
done
# Signed URLs for finished episodes are signed through the IAM API as this account.
g iam service-accounts add-iam-policy-binding "$RUN_SA" --member "serviceAccount:$RUN_SA" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null

say "Building and pushing $IMAGE"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet >/dev/null
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE"

say "Deploying Cloud Run service: $SERVICE ($REGION)"
# One instance on purpose: live rooms are in memory (see "Scaling" in the README).
# --timeout 3600: WebSockets live up to an hour; clients reconnect seamlessly after.
# --no-cpu-throttling: the pod keeps writing and voicing between requests.
# "^|^" switches gcloud's list separator to "|" so ADMIN_EMAILS can contain commas.
ENV_VARS="^|^STORAGE_BACKEND=gcp|GCS_BUCKET=$BUCKET|GCP_PROJECT=$PROJECT_ID|COOKIE_SECURE=true"
ENV_VARS+="|AUTH_MODE=firebase|FIREBASE_PROJECT_ID=$PROJECT_ID|FIREBASE_API_KEY=$FIREBASE_API_KEY"
ENV_VARS+="|FIREBASE_AUTH_DOMAIN=$FIREBASE_AUTH_DOMAIN|ADMIN_EMAILS=$ADMIN_EMAILS|SIGNUP_CREDITS=$SIGNUP_CREDITS"
g run deploy "$SERVICE" \
  --image "$IMAGE" --region "$REGION" --service-account "$RUN_SA" \
  --allow-unauthenticated --execution-environment gen2 \
  --cpu 2 --memory 2Gi --no-cpu-throttling --cpu-boost \
  --timeout 3600 --concurrency 250 --min-instances 0 --max-instances 1 --session-affinity \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,SESSION_SECRET=session-secret:latest"

URL="$(g run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
say "Checking $URL/healthz"
curl -fsS "$URL/healthz"
echo
echo
echo "Gemini Podcasts is live at $URL"
echo
echo "One-time step if you haven't yet: let Firebase sign people in on that address."
echo "  Firebase console > Authentication > Settings > Authorized domains > Add domain:"
echo "  ${URL#https://}"
echo "Then sign in with one of the ADMIN_EMAILS and grant credits from the Admin page."
