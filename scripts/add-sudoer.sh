#!/usr/bin/env bash
# Grant the current user passwordless sudo for the one command the web UI needs:
#   sudo systemctl restart video-home-server.service
# Run as root: sudo bash scripts/add-sudoer.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

# When invoked via sudo, SUDO_USER is the original unprivileged user.
TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" ]]; then
  echo "Could not determine the target user. Run with sudo, not as root directly." >&2
  exit 1
fi

RULE_FILE="/etc/sudoers.d/video-home-server"
RULE="${TARGET_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart video-home-server.service"

if [[ -f "$RULE_FILE" ]] && grep -qF "$RULE" "$RULE_FILE"; then
  echo "Rule already present for ${TARGET_USER}."
  exit 0
fi

# Write to a temp file and validate before installing.
TMP="$(mktemp)"
echo "$RULE" > "$TMP"
chmod 0440 "$TMP"

if ! visudo -c -f "$TMP" &>/dev/null; then
  echo "visudo validation failed — rule not installed." >&2
  rm -f "$TMP"
  exit 1
fi

mv "$TMP" "$RULE_FILE"
echo "Added sudoers rule for ${TARGET_USER}:"
echo "  ${RULE}"
