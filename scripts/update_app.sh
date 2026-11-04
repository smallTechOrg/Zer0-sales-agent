#!/bin/bash
# LEGACY: VM deploy only. Cloud Run (.github/workflows/deploy.yml) does not use
# this script. Delete it once the VMs are gone - see DEPLOYMENT.md.
# update_app.sh
# Cleans old logs, runs on VM startup, pulls latest code, and triggers deploy.sh

set -euo pipefail

# Re-run as root in a non-interactive way if needed.
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  exec sudo -n "$0" "$@"
fi

# Clean or rotate old logs
LOG_FILE="/var/log/update_app.log"

# Clean old log file if exists
if [ -f "$LOG_FILE" ]; then
  echo "Removing old log file..."
  rm -f "$LOG_FILE"
fi



# Start logging
echo "==== $(date): Starting update_app.sh ====" | tee -a "$LOG_FILE"
exec >> "$LOG_FILE" 2>&1
set -x

cd /opt/ai-agent-boilerplate

# Read branch name from metadata
BRANCH=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/attributes/BRANCH_NAME" -H "Metadata-Flavor: Google")
echo "Pulling latest code from branch: $BRANCH"

echo "Pulling latest code..."
git -c safe.directory=/opt/ai-agent-boilerplate fetch --all
git -c safe.directory=/opt/ai-agent-boilerplate reset --hard origin/$BRANCH
git -c safe.directory=/opt/ai-agent-boilerplate clean -fd

echo "Running deploy.sh..." | tee -a "$LOG_FILE"
chmod +x scripts/deploy.sh
./scripts/deploy.sh >> "$LOG_FILE" 2>&1
