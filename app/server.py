"""Flask web app for the video home server."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from .downloader import Downloader, get_formats
from .library import Library, LibraryError

VIDEOS_DIR = Path(os.environ.get("VIDEOS_DIR", "/home/pi/videos"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))
SETTINGS_FILE = VIDEOS_DIR.parent / ".vhs_settings.json"


def _read_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_settings(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")


def create_app() -> Flask:
    app = Flask(__name__)
    library = Library(VIDEOS_DIR)
    downloader = Downloader(VIDEOS_DIR, max_workers=MAX_CONCURRENT_DOWNLOADS)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/formats")
    def api_formats():
        data = request.get_json(silent=True) or request.form
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400
        cookies = (data.get("cookies") or "").strip() or None
        try:
            return jsonify(get_formats(url, cookies=cookies))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/download")
    def api_download():
        data = request.get_json(silent=True) or request.form
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400
        format_id = (data.get("format_id") or "").strip() or None
        cookies = (data.get("cookies") or "").strip() or None
        custom_filename = (data.get("custom_filename") or "").strip() or None
        start_time_raw = data.get("start_time")
        end_time_raw = data.get("end_time")
        try:
            start_time = float(start_time_raw) if start_time_raw is not None else None
            end_time = float(end_time_raw) if end_time_raw is not None else None
        except (TypeError, ValueError):
            return jsonify({"error": "start_time and end_time must be numbers"}), 400
        job = downloader.submit(url, format_id=format_id, cookies=cookies,
                                start_time=start_time, end_time=end_time,
                                custom_filename=custom_filename)
        return jsonify({"id": job.id, "url": job.url, "status": job.status})

    @app.get("/api/settings")
    def api_get_settings():
        return jsonify(_read_settings())

    @app.post("/api/settings")
    def api_save_settings():
        data = request.get_json(silent=True) or {}
        settings = _read_settings()
        settings.update(data)
        _write_settings(settings)
        return jsonify({"ok": True})

    @app.get("/api/downloads")
    def api_downloads():
        return jsonify({"jobs": downloader.snapshot()})

    @app.get("/api/library")
    def api_library():
        return jsonify({"videos": library.list_videos()})

    @app.delete("/api/library/<path:name>")
    def api_delete(name: str):
        try:
            library.delete(name)
        except LibraryError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.get("/files/<path:name>")
    def api_file(name: str):
        # send_from_directory itself blocks traversal, but be explicit.
        return send_from_directory(VIDEOS_DIR, name, as_attachment=True)

    @app.post("/api/admin/update-ytdlp")
    def api_update_ytdlp():
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                capture_output=True, text=True, timeout=120,
            )
            output = (result.stdout + result.stderr).strip()
            return jsonify({"ok": result.returncode == 0, "output": output})
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "output": "Timed out after 120 s"}), 500

    @app.post("/api/admin/restart")
    def api_restart():
        def _do() -> None:
            import time
            time.sleep(1)
            subprocess.run(["sudo", "systemctl", "restart", "video-home-server.service"])
        threading.Thread(target=_do, daemon=True).start()
        return jsonify({"ok": True})

    return app


def main() -> None:
    app = create_app()
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
