#!/usr/bin/env bash
# Idempotent installer for the Video Home Server on Raspberry Pi OS.
# Run as root (sudo) from the repo root: sudo bash scripts/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/video-home-server"
USB_MOUNT="/mnt/usb"
VIDEOS_DIR="${USB_MOUNT}/videos"
PI_USER="pi"

echo "==> Installing apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-venv python3-pip \
  ffmpeg \
  vsftpd \
  samba samba-common-bin \
  avahi-daemon

echo "==> Creating USB mount point at ${USB_MOUNT}"
mkdir -p "${USB_MOUNT}"

# Resolve the numeric uid/gid for PI_USER so we can embed them in the fstab hint.
PI_UID="$(id -u "${PI_USER}" 2>/dev/null || echo 1000)"
PI_GID="$(id -g "${PI_USER}" 2>/dev/null || echo 1000)"

echo "    NOTE: Add your USB drive to /etc/fstab to auto-mount it at ${USB_MOUNT}."
echo "    For exFAT drives (most USB sticks) use uid/gid so the service can write:"
echo "    UUID=<your-uuid>  ${USB_MOUNT}  exfat  defaults,nofail,uid=${PI_UID},gid=${PI_GID},fmask=0133,dmask=0022  0  0"
echo "    For ext4 drives the simpler form works:"
echo "    UUID=<your-uuid>  ${USB_MOUNT}  ext4  defaults,nofail  0  2"

echo "==> Creating videos directory at ${VIDEOS_DIR}"
# exFAT doesn't support chown; ownership comes from mount options instead.
# Try the ownership-aware install first; fall back to plain mkdir on exFAT.
if ! install -d -o "${PI_USER}" -g "${PI_USER}" -m 0755 "${VIDEOS_DIR}" 2>/dev/null; then
  mkdir -p "${VIDEOS_DIR}"
fi

# Verify the service user can actually write here; fail loudly if not.
if ! sudo -u "${PI_USER}" test -w "${VIDEOS_DIR}" 2>/dev/null; then
  echo ""
  echo "  WARNING: ${VIDEOS_DIR} is not writable by '${PI_USER}'."
  echo "  Downloads will fail with 'Permission denied' until this is fixed."
  echo "  If the drive is exFAT, re-mount it with uid=${PI_UID},gid=${PI_GID} (see fstab hint above)."
  echo ""
fi

echo "==> Syncing repo to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
# Copy app code + templates + configs; exclude the venv if re-running.
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' --exclude 'videos' \
  "${REPO_DIR}/" "${INSTALL_DIR}/"
chown -R "${PI_USER}:${PI_USER}" "${INSTALL_DIR}"

echo "==> Creating Python venv"
sudo -u "${PI_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
sudo -u "${PI_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${PI_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> Configuring vsftpd"
if [[ -f /etc/vsftpd.conf && ! -f /etc/vsftpd.conf.orig ]]; then
  cp /etc/vsftpd.conf /etc/vsftpd.conf.orig
fi
install -m 0644 "${INSTALL_DIR}/config/vsftpd.conf" /etc/vsftpd.conf
systemctl enable vsftpd
systemctl restart vsftpd

echo "==> Configuring Samba"
SMB_MARK="# === video-home-server share ==="
if ! grep -qF "${SMB_MARK}" /etc/samba/smb.conf; then
  if [[ ! -f /etc/samba/smb.conf.orig ]]; then
    cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
  fi
  {
    echo ""
    echo "${SMB_MARK}"
    cat "${INSTALL_DIR}/config/smb.conf.snippet"
  } >> /etc/samba/smb.conf
fi
# Ensure guest mapping is on so VLC's anonymous browse works.
if ! grep -qE '^\s*map to guest\s*=' /etc/samba/smb.conf; then
  sed -i '/^\[global\]/a \   map to guest = Bad User' /etc/samba/smb.conf
fi
systemctl enable smbd
systemctl restart smbd

echo "==> Enabling avahi (mDNS)"
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
