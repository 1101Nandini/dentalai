#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# ==========================================
# Deployment Configuration & Validation
# ==========================================
MISSING_VARS=()
[ -z "$SSH_USER" ] && MISSING_VARS+=("SSH_USER")
[ -z "$SSH_HOST" ] && MISSING_VARS+=("SSH_HOST")
[ -z "$REMOTE_DIR" ] && MISSING_VARS+=("REMOTE_DIR")

if [ ${#MISSING_VARS[@]} -ne 0 ]; then
  echo "Error: The following required environment variables are missing:"
  for var in "${MISSING_VARS[@]}"; do
    echo "  - $var"
  done
  echo "Please export these variables before running this script (e.g. export SSH_HOST=192.168.0.172)."
  exit 1
fi

PM2_APP_ID="${PM2_APP_ID:-Petal-AI-Backend}"
SSH_KEY="${SSH_KEY:-}" # e.g., ~/.ssh/id_rsa

# Dynamically construct SSH options using an array to safely handle spaces in key paths
SSH_OPTS=()
if [ -n "$SSH_KEY" ]; then
  SSH_OPTS+=("-i" "$SSH_KEY")
fi
# ==========================================

ARCHIVE_FILE=$(mktemp /tmp/backend-XXXXXX.tar.gz)
trap 'rm -f "$ARCHIVE_FILE"' EXIT

echo "Building project archive..."
# Exclude local configuration, secrets, virtual environments, logs, and caches
tar --warning=no-file-changed -czf "$ARCHIVE_FILE" \
  --exclude='./.venv' \
  --exclude='./venv' \
  --exclude='./env' \
  --exclude='./node_modules' \
  --exclude='./.git' \
  --exclude='./.github' \
  --exclude='./.vscode' \
  --exclude='./.idea' \
  --exclude='./.pytest_cache' \
  --exclude='**/__pycache__' \
  --exclude='./.env*' \
  --exclude='./gcp-credentials.json' \
  --exclude='./*.log' \
  .

echo "Copying archive to remote server ($SSH_HOST)..."
scp "${SSH_OPTS[@]}" "$ARCHIVE_FILE" "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/backend.tar.gz"

echo "Extracting archive and restarting $PM2_APP_ID on remote server..."
ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SSH_HOST}" "bash -lc 'mkdir -p ${REMOTE_DIR} && cd ${REMOTE_DIR} && tar -xzf backend.tar.gz && rm -f backend.tar.gz && export NVM_DIR=\$HOME/.nvm && [ -s \$NVM_DIR/nvm.sh ] && . \$NVM_DIR/nvm.sh && pm2 restart ${PM2_APP_ID}'"

echo "Cleaning up local archive..."
rm -f "$ARCHIVE_FILE"

echo "Successfully deployed and restarted $PM2_APP_ID!"