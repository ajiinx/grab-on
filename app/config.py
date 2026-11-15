"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "downloads"))

# Optional cookies for login-gated Instagram / age-restricted YouTube content.
COOKIES_FILE = os.getenv("COOKIES_FILE") or None
# e.g. "firefox" or "chrome:Profile 1" — same syntax as yt-dlp's --cookies-from-browser.
COOKIES_FROM_BROWSER = os.getenv("COOKIES_FROM_BROWSER") or None
# Directory containing ffmpeg/ffprobe, if they are not on PATH.
FFMPEG_LOCATION = os.getenv("FFMPEG_LOCATION") or None

JS_RUNTIMES = [r.strip() for r in os.getenv("JS_RUNTIMES", "deno,node,bun").split(",") if r.strip()]

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
# Write title/artist/chapters and the thumbnail (cover art) into downloaded files.
EMBED_METADATA = os.getenv("EMBED_METADATA", "true").lower() not in ("0", "false", "no")
# Finished files are kept this long so the browser can (re)fetch them, then deleted.
FILE_TTL_MINUTES = float(os.getenv("FILE_TTL_MINUTES", "30"))

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8001"))
# If set, every request needs HTTP Basic auth with this password (any username).
APP_PASSWORD = os.getenv("APP_PASSWORD") or None
# Run `pip install -U yt-dlp` before starting the server.
YTDLP_AUTO_UPDATE = os.getenv("YTDLP_AUTO_UPDATE", "false").lower() in ("1", "true", "yes")

# Per-IP request limits (requests per minute).
RATE_LIMIT_INFO = max(1, int(os.getenv("RATE_LIMIT_INFO", "30")))
RATE_LIMIT_DOWNLOAD = max(1, int(os.getenv("RATE_LIMIT_DOWNLOAD", "10")))

# Allowed CORS origins, e.g. "https://myapp.com,https://client.myapp.com". Empty = same-origin only.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

# Reverse proxy IPs trusted to pass X-Forwarded-For headers (e.g. "127.0.0.1,::1,10.0.0.0/8").
TRUSTED_PROXIES = set(p.strip() for p in os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip())

# Maximum execution time in seconds for a single download job (prevents hung tasks).
JOB_TIMEOUT_SECONDS = max(60.0, float(os.getenv("JOB_TIMEOUT_SECONDS", "1800")))

JOBS_DIR = DOWNLOAD_DIR / "jobs"
HISTORY_DB = DATA_DIR / "history.db"


@lru_cache
def ffmpeg_path() -> str | None:
    """Return the ffmpeg executable path, or None if it can't be found."""
    if FFMPEG_LOCATION:
        loc = Path(FFMPEG_LOCATION)
        candidates = [loc] if loc.is_file() else [loc / "ffmpeg", loc / "ffmpeg.exe"]
        for c in candidates:
            if c.is_file():
                return str(c)
        return None
    return shutil.which("ffmpeg")


def ffmpeg_version() -> str | None:
    path = ffmpeg_path()
    if not path:
        return None
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = out.stdout.splitlines()[0] if out.stdout else ""
    # "ffmpeg version 6.1.1 Copyright ..." -> "6.1.1"
    parts = first.split()
    version = parts[2] if len(parts) > 2 else first
    return re.split(r"-(?:full|essentials)_build|-static", version)[0] or None


def require_ffmpeg() -> str:
    path = ffmpeg_path()
    if not path:
        raise RuntimeError(
            "ffmpeg was not found. Install it (Windows: `winget install Gyan.FFmpeg`, "
            "macOS: `brew install ffmpeg`, Debian/Ubuntu: `apt install ffmpeg`) "
            "or set FFMPEG_LOCATION to the folder containing ffmpeg."
        )
    return path


class _YdlLogger:
    """Route yt-dlp's console output into `logging` (it prints errors even when quiet)."""

    _log = logging.getLogger("yt_dlp")

    def debug(self, msg: str) -> None:
        self._log.debug(msg)

    def info(self, msg: str) -> None:
        self._log.debug(msg)

    def warning(self, msg: str) -> None:
        self._log.debug(msg)  # very chatty; real failures surface as exceptions

    def error(self, msg: str) -> None:
        self._log.info(msg)


def base_ydl_opts() -> dict[str, Any]:
    """Options shared by every yt-dlp call (info extraction and downloads)."""
    opts: dict[str, Any] = {
        "logger": _YdlLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
    }
    # YouTube needs a JS runtime to solve its player challenges; yt-dlp only
    # enables deno by default, so also allow node/bun if those are installed.
    opts["js_runtimes"] = {name: {} for name in JS_RUNTIMES}
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if COOKIES_FROM_BROWSER:
        # yt-dlp expects a tuple: (browser, profile, keyring, container)
        browser, _, profile = COOKIES_FROM_BROWSER.partition(":")
        opts["cookiesfrombrowser"] = (browser, profile or None, None, None)
    return opts
