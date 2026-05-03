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
  avahi-daemon \
  exfat-fuse

echo "==> Setting up USB mount at ${USB_MOUNT}"
mkdir -p "${USB_MOUNT}"

PI_UID="$(id -u "${PI_USER}")"
PI_GID="$(id -g "${PI_USER}")"

# Detect the UUID of the first non-system removable block device.
# Skips mmcblk (SD card) and nvme (boot SSD).
detect_usb_uuid() {
  lsblk -o NAME,UUID,HOTPLUG,TYPE -J 2>/dev/null \
    | python3 -c "
import sys, json
data = json.load(sys.stdin)
def walk(nodes):
    for n in nodes:
        if n.get('type') == 'part' and n.get('hotplug') == '1' and n.get('uuid'):
            print(n['uuid'])
            sys.exit(0)
        walk(n.get('children') or [])
walk(data.get('blockdevices', []))
" 2>/dev/null || true
}

FSTAB_MARK="# video-home-server usb"
if grep -q "${FSTAB_MARK}" /etc/fstab; then
  echo "    fstab entry already present, skipping."
else
  USB_UUID="$(detect_usb_uuid)"
  if [[ -z "${USB_UUID}" ]]; then
    echo "    WARNING: No removable USB drive detected. Plug in the drive and re-run"
    echo "    the installer, or add an fstab entry manually:"
    echo "    UUID=<your-uuid>  ${USB_MOUNT}  exfat  defaults,nofail,uid=${PI_UID},gid=${PI_GID},fmask=0133,dmask=0022  0  0"
  else
    echo "    Detected USB drive UUID=${USB_UUID}, writing fstab entry."
    echo "UUID=${USB_UUID}  ${USB_MOUNT}  exfat  defaults,nofail,uid=${PI_UID},gid=${PI_GID},fmask=0133,dmask=0022  0  0  ${FSTAB_MARK}" \
      >> /etc/fstab
    # Release any stale mounts before remounting with the correct options.
    systemctl stop vsftpd 2>/dev/null || true
    umount "${USB_MOUNT}" 2>/dev/null || true
    umount "${USB_MOUNT}" 2>/dev/null || true  # clear double-mounts
    mount "${USB_MOUNT}"
    systemctl start vsftpd 2>/dev/null || true
  fi
fi

echo "==> Creating videos directory at ${VIDEOS_DIR}"
# exFAT doesn't support chown; ownership comes from uid/gid mount options.
if ! install -d -o "${PI_USER}" -g "${PI_USER}" -m 0755 "${VIDEOS_DIR}" 2>/dev/null; then
  mkdir -p "${VIDEOS_DIR}"
fi

# Verify the service user can actually write here; fail loudly if not.
if ! sudo -u "${PI_USER}" test -w "${VIDEOS_DIR}" 2>/dev/null; then
  echo ""
  echo "  WARNING: ${VIDEOS_DIR} is not writable by '${PI_USER}'."
  echo "  If the drive is exFAT, ensure the fstab entry includes uid=${PI_UID},gid=${PI_GID}."
  echo "  Run 'sudo umount ${USB_MOUNT} && sudo mount ${USB_MOUNT}' after fixing fstab."
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
if [[ ! -f /etc/samba/smb.conf.orig ]]; then
  cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
fi
# Always remove any existing section and re-append so re-runs pick up changes.
python3 - <<'PYEOF'
import sys
path = '/etc/samba/smb.conf'
marker = '\n# === video-home-server share ==='
content = open(path).read()
idx = content.find(marker)
if idx != -1:
    content = content[:idx]
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
