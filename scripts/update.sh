#!/usr/bin/env bash
# Fast update: sync code and restart the service. Run after code changes.
# Usage: sudo bash scripts/update.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/video-home-server"
SERVICE_USER="video-server"

echo "==> Syncing repo to ${INSTALL_DIR}"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude 'videos' \
  "${REPO_DIR}/" "${INSTALL_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Syncing Python dependencies"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade \
  -r "${INSTALL_DIR}/requirements.txt"

echo "==> Restarting service"
systemctl restart video-home-server.service
systemctl --no-pager status video-home-server.service

echo "Done."
