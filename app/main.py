"""FastAPI app: JSON API, SSE progress stream and the static single-page UI."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import secrets
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, updates
from .errors import UserFacingError
from .extractor import get_media_info, playlist_link, validate_url
from .jobs import TERMINAL_STAGES, JobManager
from .models import DownloadRequest, Health, HistoryItem, InfoRequest, JobCreated, MediaInfo, ProgressEvent
from .storage import History, clean_jobs_dir, zip_stream

log = logging.getLogger("app")
STATIC_DIR = Path(__file__).parent / "static"
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------- #
# Small helpers: info cache, rate limiting
# --------------------------------------------------------------------------- #


class InfoCache:
    """Keeps recent /api/info results so the download step doesn't re-extract."""

    def __init__(self, ttl: float = 15 * 60, max_items: int = 200):
        self.ttl, self.max_items = ttl, max_items
        self._items: dict[str, tuple[float, MediaInfo]] = {}

    def get(self, url: str) -> MediaInfo | None:
        hit = self._items.get(url)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        self._items.pop(url, None)
        return None

    def put(self, url: str, info: MediaInfo) -> None:
        if len(self._items) >= self.max_items:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            self._items.pop(oldest, None)
        self._items[url] = (time.monotonic(), info)


class RateLimiter:
    """Sliding-window limiter: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


info_cache = InfoCache()
info_limiter = RateLimiter(config.RATE_LIMIT_INFO)
download_limiter = RateLimiter(config.RATE_LIMIT_DOWNLOAD)
auth_fail_limiter = RateLimiter(10)
update_status: dict[str, str | None] = {"latest": None}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_rate(limiter: RateLimiter, request: Request) -> None:
    if not limiter.allow(client_ip(request)):
        raise UserFacingError("rate_limited", "Slow down a little — too many requests. Try again in a minute.")


async def lookup_info(raw_url: str) -> MediaInfo:
    _, url = validate_url(raw_url)
    info = info_cache.get(url)
    if info is None:
        info = await asyncio.to_thread(get_media_info, url)
        info_cache.put(url, info)
    # The cache is keyed by the canonical URL, but "this video is part of a
    # playlist" depends on the link that was actually pasted.
    pl = playlist_link(raw_url) if info.kind == "video" and info.platform == "youtube" else None
    return info.model_copy(update={"playlist_url": pl})


# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #


async def _check_updates() -> None:
    latest = await asyncio.to_thread(updates.latest_version)
    update_status["latest"] = latest
    current = updates.installed_version()
    if updates.is_newer(latest, current):
        log.warning("yt-dlp %s is available (installed: %s). Update with: pip install -U yt-dlp", latest, current)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.ffmpeg_path():
        log.error("ffmpeg NOT FOUND — downloads will fail. %s", "Install ffmpeg or set FFMPEG_LOCATION.")
    if config.COOKIES_FILE and not Path(config.COOKIES_FILE).is_file():
        log.warning("COOKIES_FILE is set but %s does not exist", config.COOKIES_FILE)
    await asyncio.to_thread(clean_jobs_dir, config.JOBS_DIR)

    history = History(config.HISTORY_DB)
    manager = JobManager(
        config.JOBS_DIR,
        config.MAX_CONCURRENT_DOWNLOADS,
        history,
        lookup_info,
        config.FILE_TTL_MINUTES * 60,
    )
    app.state.history = history
    app.state.jobs = manager
    background = [asyncio.create_task(manager.prune_loop()), asyncio.create_task(_check_updates())]
    try:
        yield
    finally:
        for t in background:
            t.cancel()
        await manager.shutdown()
        history.close()


app = FastAPI(title="Media Downloader", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(UserFacingError)
async def user_error_handler(_: Request, exc: UserFacingError) -> JSONResponse:
    status = {"rate_limited": 429, "busy": 429, "not_found": 404}.get(exc.code, 400)
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=status)


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": "http", "message": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)


def _password_ok(header: str | None) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    _, _, password = decoded.partition(":")  # any username is accepted
    return secrets.compare_digest(password.encode(), (config.APP_PASSWORD or "").encode())


@app.middleware("http")
async def auth_and_headers(request: Request, call_next):
    if config.APP_PASSWORD:
        if not _password_ok(request.headers.get("authorization")):
            ip = client_ip(request)
            if request.headers.get("authorization") and not auth_fail_limiter.allow(ip):
                return JSONResponse({"error": "rate_limited", "message": "Too many failed logins."}, status_code=429)
            return Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Media Downloader", charset="UTF-8"'},
            )
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    )
    return response


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

# Internal fields the UI doesn't need.
_INFO_EXCLUDE = {"options": {"__all__": {"format", "format_sort", "stream_sizes"}}}


@app.post("/api/info", response_model=MediaInfo, response_model_exclude=_INFO_EXCLUDE)
async def api_info(body: InfoRequest, request: Request) -> MediaInfo:
    check_rate(info_limiter, request)
    return await lookup_info(body.url)


@app.post("/api/download", response_model=JobCreated)
async def api_download(body: DownloadRequest, request: Request) -> JobCreated:
    check_rate(download_limiter, request)
    _, url = validate_url(body.url)
    job = request.app.state.jobs.submit(url, body.option_id, items=body.items, subtitle=body.subtitle)
    return JobCreated(job_id=job.id)


def _get_job(request: Request, job_id: str):
    job = request.app.state.jobs.get(job_id) if _JOB_ID.match(job_id) else None
    if job is None:
        raise UserFacingError("not_found", "That download has expired or doesn't exist.")
    return job


@app.get("/api/progress/{job_id}")
async def api_progress(job_id: str, request: Request) -> StreamingResponse:
    job = _get_job(request, job_id)

    async def stream():
        while True:
            changed = job.changed()  # capture before reading state to avoid missing an update
            state: ProgressEvent = job.state
            yield f"data: {state.model_dump_json(exclude_none=True)}\n\n"
            if state.stage in TERMINAL_STAGES:
                return
            try:
                await asyncio.wait_for(changed.wait(), timeout=15)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    return
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}", response_model=ProgressEvent, response_model_exclude_none=True)
async def api_job(job_id: str, request: Request) -> ProgressEvent:
    return _get_job(request, job_id).state


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel(job_id: str, request: Request) -> dict:
    job = _get_job(request, job_id)
    return {"cancelled": request.app.state.jobs.cancel(job.id)}


_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".srt": "application/x-subrip",
}


def _finished_files(request: Request, job_id: str) -> tuple:
    job = _get_job(request, job_id)
    if job.state.stage != "done" or not job.files or not all(p.is_file() for _, p in job.files):
        raise UserFacingError("not_found", "The file isn't ready, or it has already been cleaned up.")
    return job, job.files


def _serve(path: Path, name: str) -> FileResponse:
    # FileResponse streams from disk in chunks and supports Range requests.
    return FileResponse(path, filename=name, media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"))


@app.get("/api/file/{job_id}", response_model=None)
async def api_file(job_id: str, request: Request) -> FileResponse | StreamingResponse:
    """The finished file; for multi-item playlist jobs, a ZIP of all files."""
    job, files = _finished_files(request, job_id)
    if len(files) == 1:
        return _serve(files[0][1], job.download_name or files[0][0])
    # Built on the fly while streaming — no temp copy, no size limit.
    return StreamingResponse(
        zip_stream(files),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(job.download_name or 'playlist.zip')}"},
    )


@app.get("/api/file/{job_id}/{number}")
async def api_file_item(job_id: str, number: int, request: Request) -> FileResponse:
    """One file of a playlist job (1-based, in the order of ProgressEvent.files)."""
    _, files = _finished_files(request, job_id)
    if not 1 <= number <= len(files):
        raise UserFacingError("not_found", "No such file in this download.")
    name, path = files[number - 1]
    return _serve(path, name)


@app.get("/api/history", response_model=list[HistoryItem])
async def api_history(request: Request) -> list[HistoryItem]:
    return await asyncio.to_thread(request.app.state.history.list)


@app.delete("/api/history/{item_id}")
async def api_history_delete(item_id: int, request: Request) -> dict:
    if not await asyncio.to_thread(request.app.state.history.delete, item_id):
        raise UserFacingError("not_found", "History entry not found.")
    return {"deleted": True}


@app.get("/api/health", response_model=Health)
async def api_health() -> Health:
    ffmpeg_ver = await asyncio.to_thread(config.ffmpeg_version)
    current = updates.installed_version()
    latest = update_status["latest"]
    return Health(
        status="ok" if ffmpeg_ver else "degraded",
        yt_dlp=current,
        yt_dlp_latest=latest,
        yt_dlp_update_available=updates.is_newer(latest, current),
        ffmpeg=ffmpeg_ver,
        ffmpeg_ok=ffmpeg_ver is not None,
        cookies_configured=bool(config.COOKIES_FILE or config.COOKIES_FROM_BROWSER),
        auth_enabled=bool(config.APP_PASSWORD),
        max_concurrent_downloads=config.MAX_CONCURRENT_DOWNLOADS,
    )


# --------------------------------------------------------------------------- #
# Thumbnail proxy — Instagram's CDN blocks cross-site <img> loads, and proxying
# keeps the page free of third-party requests. Strict host allow-list (no SSRF).
# --------------------------------------------------------------------------- #

THUMB_HOSTS = (
    "ytimg.com",
    "ggpht.com",
    "googleusercontent.com",
    "cdninstagram.com",
    "fbcdn.net",
    "spotifycdn.com",
    "scdn.co",
    "twimg.com",
    "twitter.com",
    "x.com",
)
THUMB_MAX_BYTES = 5 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # redirects could leave the allow-list
        return None


_thumb_opener = urllib.request.build_opener(_NoRedirect)


def _thumb_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and parts.port in (None, 443)
        and not parts.username
        and any(host == h or host.endswith("." + h) for h in THUMB_HOSTS)
    )


def _fetch_thumb(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _thumb_opener.open(req, timeout=10) as resp:
        ctype = resp.headers.get_content_type()
        if not ctype.startswith("image/"):
            raise ValueError("not an image")
        data = resp.read(THUMB_MAX_BYTES + 1)
    if len(data) > THUMB_MAX_BYTES:
        raise ValueError("too large")
    return data, ctype


@app.get("/api/thumb")
async def api_thumb(u: str = Query(max_length=4096)) -> Response:
    if not _thumb_allowed(u):
        raise HTTPException(400, "Thumbnail host not allowed")
    try:
        data, ctype = await asyncio.to_thread(_fetch_thumb, u)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        raise HTTPException(502, "Couldn't load thumbnail") from None
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
