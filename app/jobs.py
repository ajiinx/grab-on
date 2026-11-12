"""Background job queue: runs downloads in worker threads with a concurrency limit.

Each job gets its own folder under JOBS_DIR. Progress events from the worker
thread are handed to the event loop and fanned out to SSE subscribers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import spotify
from .downloader import DownloadCancelled, download
from .errors import UserFacingError, friendly_error
from .extractor import default_subtitle
from .models import DownloadOption, MediaInfo, PlaylistEntry, ProgressEvent
from .storage import History, remove_dir

log = logging.getLogger(__name__)

TERMINAL_STAGES = {"done", "error", "cancelled"}
MAX_PENDING_JOBS = 20

InfoLookup = Callable[[str], Awaitable[MediaInfo]]
Fetcher = Callable[[PlaylistEntry, Path, Callable[[ProgressEvent], None]], tuple[Path, str]]


@dataclass
class Job:
    id: str
    url: str
    option_id: str
    dir: Path
    items: list[int] | None = None  # playlist item indices
    subtitle: str | None = None
    state: ProgressEvent = field(default_factory=lambda: ProgressEvent(stage="queued", message="Waiting in queue…"))
    # Finished files as (download name, path). One file is served directly;
    # several are served as a ZIP named `download_name`.
    files: list[tuple[str, Path]] = field(default_factory=list)
    download_name: str | None = None
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    # Replaced on every publish; subscribers await the one they captured.
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_finished(self) -> bool:
        return self.state.stage in TERMINAL_STAGES

    def publish(self, event: ProgressEvent) -> None:
        """Must be called on the event loop thread."""
        if self.is_finished:
            return  # never overwrite a terminal state
        self.state = event
        if event.stage in TERMINAL_STAGES:
            self.finished_at = time.time()
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def changed(self) -> asyncio.Event:
        return self._changed


def safe_name(text: str, max_len: int = 150) -> str:
    """Strip characters that are invalid in filenames on Windows/macOS/Linux."""
    name = re.sub(r'[\x00-\x1f\x7f"\\/:*?<>|]+', "", text).strip(" .")
    return name[:max_len].rstrip(" .") or "download"


def download_filename(path: Path) -> str:
    """Name for the browser: "Title [videoid].mp4" -> "Title.mp4", stripped of risky characters.

    Subtitle files keep their language: "Title [id].en.srt" -> "Title.en.srt".
    """
    stem, suffix = path.stem, path.suffix
    lang = re.search(r"\.([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)$", stem) if suffix.lower() == ".srt" else None
    if lang:
        stem, suffix = stem[: lang.start()], f".{lang.group(1)}{suffix}"
    stem = re.sub(r"\s*\[[A-Za-z0-9_-]{5,}\]$", "", stem)
    return f"{safe_name(stem)}{suffix}"


class JobManager:
    def __init__(self, jobs_dir: Path, max_concurrent: int, history: History, info_lookup: InfoLookup, file_ttl_seconds: float):
        self.jobs_dir = jobs_dir
        self.history = history
        self.info_lookup = info_lookup
        self.file_ttl = file_ttl_seconds
        self.jobs: dict[str, Job] = {}
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task] = set()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def submit(self, url: str, option_id: str, items: list[int] | None = None, subtitle: str | None = None) -> Job:
        pending = sum(1 for j in self.jobs.values() if not j.is_finished)
        if pending >= MAX_PENDING_JOBS:
            raise UserFacingError("busy", "Too many downloads queued. Wait for some to finish.")
        job_id = uuid.uuid4().hex
        job = Job(id=job_id, url=url, option_id=option_id, dir=self.jobs_dir / job_id, items=items, subtitle=subtitle)
        self.jobs[job_id] = job
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.is_finished:
            return False
        job.cancel.set()
        if job.state.stage == "queued":
            job.publish(ProgressEvent(stage="cancelled", message="Cancelled."))
        return True

    async def _run(self, job: Job) -> None:
        async with self._sem:
            if job.cancel.is_set():
                job.publish(ProgressEvent(stage="cancelled", message="Cancelled."))
                return
            try:
                await self._download(job)
            except DownloadCancelled:
                job.publish(ProgressEvent(stage="cancelled", message="Cancelled."))
            except Exception as exc:  # noqa: BLE001 — report every failure to the user
                err = friendly_error(exc)
                if err.code == "unknown":
                    log.exception("Job %s failed", job.id)
                job.publish(ProgressEvent(stage="error", message=err.message, error_code=err.code))
            finally:
                if job.state.stage != "done":
                    await asyncio.to_thread(remove_dir, job.dir)

    def _resolve(self, job: Job, info: MediaInfo) -> tuple[DownloadOption, str | None]:
        """Check the request against the video's info: option, subtitle track."""
        option = next((o for o in info.options if o.id == job.option_id), None)
        if option is None:
            raise UserFacingError("bad_option", "That format isn't available for this video any more.")
        subtitle = job.subtitle
        track_ids = {t.id for t in info.subtitles}
        if subtitle and subtitle not in track_ids:
            raise UserFacingError("bad_subtitle", "Those subtitles aren't available for this video.")
        if option.kind == "subtitle":
            subtitle = subtitle or default_subtitle(info.subtitles)
            if not subtitle:
                raise UserFacingError("bad_subtitle", "This video has no subtitles.")
        elif option.kind != "video":
            subtitle = None  # subtitles only go into video files
        return option, subtitle

    async def _download(self, job: Job) -> None:
        job.publish(ProgressEvent(stage="downloading", percent=0.0, message="Reading video info…"))
        info = await self.info_lookup(job.url)
        option, subtitle = self._resolve(job, info)
        if info.platform == "spotify" and info.kind == "playlist":
            await self._download_playlist(job, info, option, self._spotify_fetcher(job, info, option))
        elif info.platform == "spotify":
            await self._download_spotify_track(job, info, option)
        elif info.kind == "playlist":
            await self._download_playlist(job, info, option, self._video_fetcher(job, info, option))
        else:
            await self._download_single(job, info, option, subtitle)

    def _emitter(self, job: Job, transform: Callable[[ProgressEvent], ProgressEvent] | None = None):
        loop = asyncio.get_running_loop()

        def emit(ev: ProgressEvent) -> None:
            # Called from the worker thread. "done" is published by the job
            # itself once files are registered, so a client can't fetch too early.
            if ev.stage != "done":
                loop.call_soon_threadsafe(job.publish, transform(ev) if transform else ev)

        return emit

    async def _download_single(self, job: Job, info: MediaInfo, option: DownloadOption, subtitle: str | None) -> None:
        # A video picked out of an Instagram carousel is fetched by its index.
        items = str(info.entries[0].index) if len(info.entries) == 1 and info.entries[0].url is None else None
        path = await asyncio.to_thread(
            download, info.url, option, job.dir, self._emitter(job), job.cancel, subtitle=subtitle, playlist_items=items
        )
        name = download_filename(path)
        job.files = [(name, path)]
        job.download_name = name
        size = path.stat().st_size
        await self._record(info, option, path.suffix, size, None)
        job.publish(ProgressEvent(stage="done", percent=100.0, total_bytes=size, filename=name))

    async def _download_spotify_track(self, job: Job, info: MediaInfo, option: DownloadOption) -> None:
        assert info.spotify is not None
        path = await asyncio.to_thread(
            spotify.download_track, info.spotify, option, job.dir, self._emitter(job), job.cancel, info.match
        )
        job.files = [(path.name, path)]  # already "Artist - Title.ext"
        job.download_name = path.name
        size = path.stat().st_size
        await self._record(info, option, path.suffix, size, None)
        job.publish(ProgressEvent(stage="done", percent=100.0, total_bytes=size, filename=path.name))

    # A fetcher downloads one playlist entry into a folder (in a worker thread)
    # and returns (file path, name to show/zip it under).
    def _video_fetcher(self, job: Job, info: MediaInfo, option: DownloadOption) -> Fetcher:
        def fetch(entry: PlaylistEntry, out_dir: Path, emit) -> tuple[Path, str]:
            # Instagram carousel items have no URL of their own: post URL + index.
            url = entry.url or info.url
            items = None if entry.url else str(entry.index)
            path = download(url, option, out_dir, emit, job.cancel, playlist_items=items)
            return path, download_filename(path)

        return fetch

    def _spotify_fetcher(self, job: Job, info: MediaInfo, option: DownloadOption) -> Fetcher:
        def fetch(entry: PlaylistEntry, out_dir: Path, emit) -> tuple[Path, str]:
            album_cover = info.thumbnail if info.album else None
            track = spotify.track_for_entry(entry, info.album, album_cover)
            path = spotify.download_track(track, option, out_dir, emit, job.cancel)
            return path, path.name

        return fetch

    async def _download_playlist(self, job: Job, info: MediaInfo, option: DownloadOption, fetch: Fetcher) -> None:
        by_index = {e.index: e for e in info.entries}
        wanted = job.items or list(by_index)
        missing = [i for i in wanted if i not in by_index]
        if missing:
            raise UserFacingError("bad_items", "Some selected items aren't in this playlist any more. Reload and try again.")
        selected: list[PlaylistEntry] = [by_index[i] for i in sorted(set(wanted))]
        total = len(selected)
        width = max(2, len(str(total)))
        failures: list[str] = []

        for n, entry in enumerate(selected):
            if job.cancel.is_set():
                raise DownloadCancelled()
            label = f"{n + 1}/{total} · {entry.title}"

            def scale(ev: ProgressEvent, n: int = n, label: str = label) -> ProgressEvent:
                # Map this item's 0-100% onto its slice of the overall bar.
                item_pct = ev.percent if ev.percent is not None and ev.stage == "downloading" else 100.0
                overall = 100.0 * (n + item_pct / 100.0) / total
                msg = label if ev.stage == "downloading" else f"{label} — {ev.message or ev.stage}"
                return ev.model_copy(update={"percent": round(min(overall, 99.9), 1), "message": msg})

            emit = self._emitter(job, scale)
            emit(ProgressEvent(stage="downloading", percent=0.0))
            item_dir = job.dir / f"{n + 1:0{width}d}"
            try:
                path, name = await asyncio.to_thread(fetch, entry, item_dir, emit)
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 — skip failed items, keep going
                err = friendly_error(exc)
                log.info("Playlist item %s failed: %s", entry.index, err.message)
                failures.append(f"{entry.title}: {err.message}")
                continue
            job.files.append((f"{n + 1:0{width}d} - {name}", path))

        if not job.files:
            first = failures[0] if failures else "unknown error"
            raise UserFacingError("playlist_failed", f"None of the {total} items could be downloaded ({first}).")

        size = sum(p.stat().st_size for _, p in job.files)
        if len(job.files) == 1:
            job.download_name = job.files[0][0].split(" - ", 1)[-1]
        else:
            job.download_name = f"{safe_name(info.title, 100)}.zip"
        await self._record(info, option, Path(job.download_name).suffix if len(job.files) == 1 else ".zip", size, len(job.files))
        ok = len(job.files)
        message = f"{ok} of {total} downloaded" + (f" · {len(failures)} failed" if failures else "")
        job.publish(
            ProgressEvent(
                stage="done",
                percent=100.0,
                total_bytes=size,
                filename=job.download_name,
                files=[name for name, _ in job.files],
                message=message,
            )
        )

    async def _record(self, info: MediaInfo, option: DownloadOption, suffix: str, size: int, item_count: int | None) -> None:
        await asyncio.to_thread(
            self.history.add,
            url=info.url,
            platform=info.platform,
            title=info.title,
            uploader=info.uploader,
            thumbnail=info.thumbnail,
            option_id=option.id,
            option_label=option.label,
            ext=suffix.lstrip(".").lower(),
            filesize=size,
            item_count=item_count,
        )

    async def prune_loop(self) -> None:
        """Delete finished jobs (and their files) once they're older than the TTL."""
        while True:
            await asyncio.sleep(60)
            await self.prune()

    async def prune(self, now: float | None = None) -> None:
        now = now or time.time()
        for job_id, job in list(self.jobs.items()):
            if job.finished_at and now - job.finished_at > self.file_ttl:
                await asyncio.to_thread(remove_dir, job.dir)
                self.jobs.pop(job_id, None)

    async def shutdown(self) -> None:
        for job in self.jobs.values():
            job.cancel.set()
        for task in list(self._tasks):
            task.cancel()
