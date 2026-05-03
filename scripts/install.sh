#!/usr/bin/env bash
# Idempotent installer for the Video Home Server on Ubuntu Server.
# Run as root (sudo) from the repo root: sudo bash scripts/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/video-home-server"
SERVICE_USER="video-server"
VIDEOS_DIR="/home/${SERVICE_USER}/videos"

echo "==> Installing apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-venv python3-pip \
  ffmpeg \
  vsftpd \
  samba samba-common-bin \
  avahi-daemon

echo "==> Creating service user '${SERVICE_USER}'"
if ! id "${SERVICE_USER}" &>/dev/null; then
  useradd -r -m -s /bin/bash "${SERVICE_USER}"
  echo "    Created user ${SERVICE_USER}."
else
  echo "    User ${SERVICE_USER} already exists, skipping."
fi

SERVICE_UID="$(id -u "${SERVICE_USER}")"
SERVICE_GID="$(id -g "${SERVICE_USER}")"

echo "==> Creating videos directory at ${VIDEOS_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0755 "${VIDEOS_DIR}"

# vsftpd's anonymous user needs to traverse /home/video-server to reach videos/.
# Ubuntu sets home dirs to 750 by default; relax just the execute bit for others.
chmod o+x "/home/${SERVICE_USER}"

# Verify the service user can actually write here; fail loudly if not.
if ! sudo -u "${SERVICE_USER}" test -w "${VIDEOS_DIR}" 2>/dev/null; then
  echo ""
  echo "  ERROR: ${VIDEOS_DIR} is not writable by '${SERVICE_USER}'." >&2
  exit 1
fi

echo "==> Syncing repo to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude 'videos' \
  "${REPO_DIR}/" "${INSTALL_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> Creating Python venv"
sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> Configuring vsftpd"
if [[ -f /etc/vsftpd.conf && ! -f /etc/vsftpd.conf.orig ]]; then
  cp /etc/vsftpd.conf /etc/vsftpd.conf.orig
fi
install -m 0644 "${INSTALL_DIR}/config/vsftpd.conf" /etc/vsftpd.conf
systemctl enable vsftpd
systemctl restart vsftpd

echo "==> Configuring Samba"
SMB_MARK="# === video-home-server share ==="
if [[ ! -f /etc/samba/smb.conf.orig ]]; then
  cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
fi
# Always remove any existing section and re-append so re-runs pick up changes.
# Also suppress default Samba shares ([homes] → "nobody", [printers], [print$])
# so VLC's media-library scanner only sees the Videos share.
python3 - <<'PYEOF'
import re
path = '/etc/samba/smb.conf'
marker = '\n# === video-home-server share ==='
content = open(path).read()
# Remove previously-appended section so re-runs are idempotent.
idx = content.find(marker)
if idx != -1:
    content = content[:idx]

# Inject "available = no" into built-in shares we want to hide.
def disable_section(text, section):
    pattern = r'(\[' + re.escape(section) + r'\][^\[]*)'
    def replacer(m):
        block = m.group(1)
        if 'available' not in block:
            lines = block.split('\n')
            lines.insert(1, '   available = no')
            return '\n'.join(lines)
        return block
    return re.sub(pattern, replacer, text, flags=re.DOTALL)

for s in ('homes', 'printers', 'print$'):
    content = disable_section(content, s)

open(path, 'w').write(content)
PYEOF
{
  echo ""
  echo "${SMB_MARK}"
  cat "${INSTALL_DIR}/config/smb.conf.snippet"
} >> /etc/samba/smb.conf
# Ensure guest mapping is on so VLC's anonymous browse works.
if ! grep -qE '^\s*map to guest\s*=' /etc/samba/smb.conf; then
  sed -i '/^\[global\]/a \   map to guest = Bad User' /etc/samba/smb.conf
fi
# nmbd (NetBIOS) is replaced by avahi for discovery; stop it if running.
systemctl disable nmbd 2>/dev/null || true
systemctl stop nmbd 2>/dev/null || true
systemctl enable smbd
systemctl restart smbd

echo "==> Enabling avahi (mDNS)"
install -m 0644 "${INSTALL_DIR}/config/avahi-smb.service" \
  /etc/avahi/services/smb.service
systemctl enable avahi-daemon
systemctl restart avahi-daemon

echo "==> Installing systemd unit"
install -m 0644 "${INSTALL_DIR}/systemd/video-home-server.service" \
  /etc/systemd/system/video-home-server.service
systemctl daemon-reload
systemctl enable video-home-server.service
systemctl restart video-home-server.service

HOSTNAME="$(hostname)"
echo
echo "Done. Open http://${HOSTNAME}.local:8080/ from your phone."
echo "VLC FTP: ftp://${HOSTNAME}.local"
echo "VLC SMB: smb://${HOSTNAME}.local/Videos"
