"""Background YouTube downloader using yt-dlp.

A single worker thread consumes a queue of jobs and writes progress into a
shared dict that the web layer reads. State is in-memory only — restarting
the service drops the job history.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from queue import Queue
from typing import Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# Prefer 1080p H.264 + AAC in MP4 (hardware-decoded on iOS).
# Fall back through progressively looser constraints.
FORMAT_SELECTOR = (
    "bv*[height<=1080][fps<=60][vcodec^=avc1]+ba[ext=m4a]"
    "/bv*[height<=1080][fps<=60]+ba"
    "/bv*[height<=1080]+ba"
    "/b[height<=1080]"
    "/b"
)

_CODEC_NAMES = {
    "avc": "H.264", "avc1": "H.264",
    "vp9": "VP9", "vp09": "VP9",
    "av01": "AV1", "av1": "AV1",
    "mp4a": "AAC",
    "opus": "Opus",
}

def _codec_short(raw: str | None) -> str | None:
    if not raw or raw == "none":
        return None
    prefix = raw.split(".")[0].lower()
    return _CODEC_NAMES.get(prefix, prefix)


def _is_netscape_cookies(s: str) -> bool:
    """Return True if *s* looks like a Netscape/Mozilla cookie file."""
    stripped = s.lstrip()
    if stripped.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        return True
    # Files exported without the header still use tab-separated columns.
    # Raw Cookie header values use '; ' and never contain tabs.
    return "\t" in stripped


@contextlib.contextmanager
def _cookie_opts(cookies: str | None):
    """Yield the ydl_opts dict fragment for the given cookie string.

    Netscape-format content (detected by header or tab separators) is written
    to a temp file; a raw Cookie header value is passed via http_headers.
    """
    if not cookies:
        yield {}
        return
    if _is_netscape_cookies(cookies):
        content = cookies
        if not cookies.lstrip().startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            content = "# Netscape HTTP Cookie File\n" + cookies
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp = f.name
        try:
            yield {"cookiefile": tmp}
        finally:
            os.unlink(tmp)
    else:
        yield {"http_headers": {"Cookie": cookies}}


def get_formats(url: str, cookies: str | None = None) -> dict:
    """Fetch available formats for *url* without downloading anything."""
    with _cookie_opts(cookies) as extra:
        ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True, **extra}
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url.strip(), download=False)

    video_formats: list[dict] = []
    audio_formats: list[dict] = []

    for f in (info.get("formats") or []):
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        fid    = f.get("format_id", "")
        size   = f.get("filesize") or f.get("filesize_approx")

        if vcodec != "none" and acodec == "none":
            video_formats.append({
                "id":     fid,
                "ext":    f.get("ext"),
                "height": f.get("height"),
                "fps":    round(f.get("fps") or 0),
                "codec":  _codec_short(vcodec),
                "vbr":    f.get("vbr"),
                "size":   size,
            })
        elif acodec != "none" and vcodec == "none":
            audio_formats.append({
                "id":    fid,
                "ext":   f.get("ext"),
                "codec": _codec_short(acodec),
                "abr":   f.get("abr"),
                "asr":   f.get("asr"),
                "size":  size,
            })

    video_formats.sort(key=lambda f: (f.get("height") or 0, f.get("fps") or 0), reverse=True)
    audio_formats.sort(key=lambda f: f.get("abr") or 0, reverse=True)

    return {
        "title":    info.get("title"),
        "duration": info.get("duration"),
        "video":    video_formats,
        "audio":    audio_formats,
    }


@dataclass
class Job:
    id: str
    url: str
    status: str = "queued"  # queued | downloading | done | error
    progress: float = 0.0   # 0..100
    title: Optional[str] = None
    filename: Optional[str] = None
    error: Optional[str] = None
    eta: Optional[int] = None
    speed: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    format_id: Optional[str] = None        # e.g. "299+140"; None → use FORMAT_SELECTOR
    cookies: Optional[str] = None          # raw Cookie header value
    stream_progress: list = field(default_factory=lambda: [0.0])  # one entry per stream
    stream_bytes: list = field(default_factory=lambda: [[0, 0]])  # [[downloaded, total], …]


class Downloader:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(self.output_dir, os.W_OK):
            raise PermissionError(
                f"Download directory is not writable: {self.output_dir}\n"
                "If the drive is exFAT, add uid=<pi-uid>,gid=<pi-gid> to its fstab mount options."
            )
        self._queue: "Queue[Job]" = Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, url: str, format_id: str | None = None, cookies: str | None = None) -> Job:
        num_streams = 2 if format_id and "+" in format_id else 1
        job = Job(id=uuid.uuid4().hex[:8], url=url.strip(), format_id=format_id,
                  cookies=cookies or None,
                  stream_progress=[0.0] * num_streams,
                  stream_bytes=[[0, 0]] * num_streams)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job

    def snapshot(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        # Newest first; keep recently-finished ones around so the UI can show them.
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        # Prune anything that finished more than 10 minutes ago.
        cutoff = time.time() - 600
        keep = [j for j in jobs if j.status in ("queued", "downloading") or j.created_at > cutoff]
        with self._lock:
            self._jobs = {j.id: j for j in keep}
        return [asdict(j) for j in keep]

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._process(job)
            except Exception as exc:  # noqa: BLE001 — log and keep the worker alive
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()

    def _process(self, job: Job) -> None:
        job.status = "downloading"
        stream_idx = 0  # increments on each "finished" event

        def hook(d: dict) -> None:
            nonlocal stream_idx
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                idx = min(stream_idx, len(job.stream_progress) - 1)
                if total:
                    job.stream_progress[idx] = round(downloaded * 100.0 / total, 1)
                    job.progress = job.stream_progress[idx]
                job.stream_bytes[idx] = [downloaded, total]
                job.eta = d.get("eta")
                job.speed = d.get("speed")
                info = d.get("info_dict") or {}
                if not job.title and info.get("title"):
                    job.title = info["title"]
            elif d.get("status") == "finished":
                idx = min(stream_idx, len(job.stream_progress) - 1)
                job.stream_progress[idx] = 100.0
                stream_idx += 1
                job.eta = None
                job.speed = None

        with _cookie_opts(job.cookies) as extra:
            ydl_opts: dict = {
                "format": job.format_id or FORMAT_SELECTOR,
                "merge_output_format": "mp4",
                "outtmpl": str(self.output_dir / "%(title)s [%(id)s].%(ext)s"),
                "noplaylist": True,
                "restrictfilenames": False,
                "windowsfilenames": True,  # avoid characters that break SMB on iOS
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [hook],
                "retries": 3,
                "fragment_retries": 3,
                **extra,
            }

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(job.url, download=True)
                    if info:
                        job.title = info.get("title") or job.title
                        final = info.get("requested_downloads") or []
                        if final:
                            job.filename = Path(final[0]["filepath"]).name
                job.status = "done"
                job.progress = 100.0
            except DownloadError as exc:
                job.status = "error"
                job.error = str(exc)
