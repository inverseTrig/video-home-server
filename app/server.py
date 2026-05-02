"""Flask web app for the video home server."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from .downloader import Downloader
from .library import Library, LibraryError

VIDEOS_DIR = Path(os.environ.get("VIDEOS_DIR", "/home/pi/videos"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))


def create_app() -> Flask:
    app = Flask(__name__)
    library = Library(VIDEOS_DIR)
    downloader = Downloader(VIDEOS_DIR)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/download")
    def api_download():
        data = request.get_json(silent=True) or request.form
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400
        job = downloader.submit(url)
        return jsonify({"id": job.id, "url": job.url, "status": job.status})

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
