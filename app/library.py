"""Filesystem-backed video library."""
from __future__ import annotations

import re
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}


class LibraryError(Exception):
    pass


class Library:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list_videos(self) -> list[dict]:
        items = []
        for entry in self.root.iterdir():
            if not entry.is_file() or entry.suffix.lower() not in VIDEO_EXTS:
                continue
            # Skip yt-dlp intermediate files: format fragments (.fNNN.ext)
            # and aria2/yt-dlp temp files (.part, .temp.ext).
            name = entry.name
            if name.endswith(".part") or ".temp." in name:
                continue
            if re.search(r'\.[f]\d+\.', name):
                continue
            stat = entry.stat()
            items.append({
                "name": entry.name,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            })
        items.sort(key=lambda i: i["mtime"], reverse=True)
        return items

    def _resolve(self, name: str) -> Path:
        # Reject obvious path-traversal attempts before touching the filesystem.
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise LibraryError("invalid filename")
        candidate = (self.root / name).resolve()
        # Final defence: make sure we never escape the videos root.
        if self.root != candidate.parent and self.root not in candidate.parents:
            raise LibraryError("invalid filename")
        return candidate

    def delete(self, name: str) -> None:
        path = self._resolve(name)
        if not path.exists():
            raise LibraryError("not found")
        if not path.is_file():
            raise LibraryError("not a file")
        path.unlink()
