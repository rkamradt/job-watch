#!/usr/bin/env bash
# Usage: MONGO_URI=... GMAIL_APP_PASSWORD=... ./scripts/create-secret.sh
set -euo pipefail

: "${MONGO_URI:?Set MONGO_URI}"
: "${GMAIL_APP_PASSWORD:?Set GMAIL_APP_PASSWORD}"
GMAIL_USER="${GMAIL_USER:-randykamradt@gmail.com}"
GMAIL_TO="${GMAIL_TO:-randykamradt@gmail.com}"

kubectl create secret generic job-watch-secrets \
  --namespace job-watch \
  --from-literal=MONGO_URI="$MONGO_URI" \
  --from-literal=GMAIL_USER="$GMAIL_USER" \
  --from-literal=GMAIL_APP_PASSWORD="$GMAIL_APP_PASSWORD" \
  --from-literal=GMAIL_TO="$GMAIL_TO" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret applied."
