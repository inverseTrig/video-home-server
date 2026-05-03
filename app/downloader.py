"""Background YouTube downloader using yt-dlp.

A single worker thread consumes a queue of jobs and writes progress into a
shared dict that the web layer reads. State is in-memory only — restarting
the service drops the job history.
"""
from __future__ import annotations

import os
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

    def submit(self, url: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], url=url.strip())
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

        def hook(d: dict) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                if total:
                    job.progress = round(downloaded * 100.0 / total, 1)
                job.eta = d.get("eta")
                job.speed = d.get("speed")
                info = d.get("info_dict") or {}
                if not job.title and info.get("title"):
                    job.title = info["title"]
            elif d.get("status") == "finished":
                # A stream finished but ffmpeg merge hasn't run yet; keep
                # progress just below 100 so the UI doesn't show done early.
                job.progress = 99.0
                job.eta = None
                job.speed = None

        ydl_opts = {
            "format": FORMAT_SELECTOR,
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
