#!/usr/bin/env bash
# Set the server's hostname so it advertises as videoserver.local on mDNS.
# Override the default by passing a name as the first arg.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

NEW_NAME="${1:-videoserver}"
CURRENT="$(hostname)"

if [[ "${CURRENT}" == "${NEW_NAME}" ]]; then
  echo "Hostname already ${NEW_NAME}."
  exit 0
fi

echo "Changing hostname: ${CURRENT} -> ${NEW_NAME}"
hostnamectl set-hostname "${NEW_NAME}"

# Keep /etc/hosts in sync so sudo doesn't complain about resolution.
if grep -qE "^127\.0\.1\.1\s" /etc/hosts; then
  sed -i -E "s/^(127\.0\.1\.1\s+).*/\1${NEW_NAME}/" /etc/hosts
else
  echo -e "127.0.1.1\t${NEW_NAME}" >> /etc/hosts
fi

systemctl restart avahi-daemon || true
echo "Reboot recommended."
