# Video Home Server

A self-hosted Raspberry Pi appliance that lets you download YouTube videos from your phone and stream them to VLC over your home network — no cloud, no account required.

**What you do:** paste a YouTube URL into Safari on your iPhone → the Pi downloads it → watch in VLC via FTP or SMB.

---

## How it works

```
iPhone Safari  ──POST /api/download──►  Flask (port 8080)
                                              │
                                         yt-dlp worker
                                              │
                                         /home/pi/videos/
                                         ┌────┴────┐
                                      vsftpd     smbd
                                      (FTP)     (SMB)
                                         └────┬────┘
                                        VLC on iPhone
```

- A single-page web UI polls every 1.5 s to show live download progress.
- yt-dlp prefers 1080p 60fps H.264 + AAC in MP4 (hardware-decoded on iOS), with graceful format fallbacks.
- FTP and SMB shares are anonymous/guest read-only — no passwords needed in VLC.
- Avahi (mDNS) advertises the Pi as `videopi.local` so you never need to type an IP.

---

## Requirements

- Raspberry Pi running Raspberry Pi OS (tested on the 64-bit Lite image)
- Default `pi` user account
- Internet access from the Pi

---

## Installation

Clone the repo to `/opt/video-home-server` and run the two setup scripts as root.

```bash
git clone https://github.com/inversetrig/video-home-server /opt/video-home-server
cd /opt/video-home-server

sudo bash scripts/set-hostname.sh        # sets hostname to videopi (see below)
sudo bash scripts/install.sh
sudo reboot
```

`set-hostname.sh` accepts an optional name argument if you want something other than `videopi`:

```bash
sudo bash scripts/set-hostname.sh mypi
```

`install.sh` is idempotent — safe to re-run after a `git pull`.

### What the installer does

| Step | Detail |
|------|--------|
| apt packages | `python3`, `ffmpeg`, `vsftpd`, `samba`, `avahi-daemon` |
| Videos dir | Creates `/home/pi/videos` owned by `pi` |
| Python venv | `/opt/video-home-server/.venv` with Flask + yt-dlp |
| vsftpd | Replaces `/etc/vsftpd.conf`; backs up original to `.orig` |
| Samba | Appends `[Videos]` share to `/etc/samba/smb.conf`; backs up original |
| systemd | Installs and enables `video-home-server.service` (runs as `pi`) |
| Avahi | Ensures mDNS is running so `.local` names resolve |

---

## Usage

### Download a video (iPhone Safari)

1. Open `http://videopi.local:8080/` in Safari.
2. Paste a YouTube URL and tap **Get**.
3. Watch the progress bar — status cycles through `queued → downloading → done`.
4. The finished file appears in the **Library** section.

The web UI also lets you delete files from the library.

### Watch in VLC (iPhone)

| Protocol | Address |
|----------|---------|
| FTP | `ftp://videopi.local` |
| SMB | `smb://videopi.local/Videos` |

Both are anonymous/guest — tap **Connect** without entering credentials.

---

## Configuration

Environment variables for the Flask service (set in `systemd/video-home-server.service`):

| Variable | Default | Description |
|----------|---------|-------------|
| `VIDEOS_DIR` | `/home/pi/videos` | Where downloads are stored |
| `HOST` | `0.0.0.0` | Flask bind address |
| `PORT` | `8080` | Flask port |

FTP passive port range is 40000–40100 (`config/vsftpd.conf`).

---

## Project layout

```
app/
  server.py          # Flask routes
  downloader.py      # yt-dlp background worker
  library.py         # Video directory + delete logic
  templates/
    index.html       # Single-page UI

config/
  vsftpd.conf        # Anonymous read-only FTP config
  smb.conf.snippet   # [Videos] SMB share (appended to smb.conf)

systemd/
  video-home-server.service

scripts/
  install.sh         # Idempotent installer
  set-hostname.sh    # Sets hostname + updates /etc/hosts

requirements.txt     # flask, yt-dlp
```

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `POST` | `/api/download` | Submit a URL (`{"url": "..."}`) |
| `GET` | `/api/downloads` | List active and recent jobs |
| `GET` | `/api/library` | List downloaded videos |
| `DELETE` | `/api/library/<name>` | Delete a video by filename |
| `GET` | `/files/<name>` | Direct file download |

---

## Security notes

- The web UI and file server have **no authentication**. Only run this on a trusted home network.
- Path traversal is blocked in `library.py` by both a name check and a resolved-path containment check.
- FTP and SMB are read-only; no uploads are possible.
- The Flask service runs as the unprivileged `pi` user.
