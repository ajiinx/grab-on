"""Run a single download with yt-dlp, reporting progress through a callback.

This module is synchronous and thread-agnostic: the API layer runs `download`
in a worker thread and forwards ProgressEvents to clients.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from .config import EMBED_METADATA, base_ydl_opts, require_ffmpeg
from .errors import friendly_error
from .models import DownloadOption, ProgressEvent

ProgressCallback = Callable[[ProgressEvent], None]

# yt-dlp postprocessor name -> stage shown to the user
_PP_STAGES = {
    "Merger": "merging",
    "FFmpegMerger": "merging",
    "ExtractAudio": "converting",
    "FFmpegExtractAudio": "converting",
    "VideoRemuxer": "processing",
    "FFmpegVideoRemuxer": "processing",
    "FFmpegMetadata": "processing",
    "Metadata": "processing",
    "EmbedThumbnail": "processing",
    "FFmpegEmbedSubtitle": "processing",
    "EmbedSubtitle": "processing",
    "FFmpegSubtitlesConvertor": "converting",
    "SubtitlesConvertor": "converting",
}
_PP_MESSAGES = {
    "Merger": "Merging video and audio…",
    "ExtractAudio": "Converting to MP3…",
    "Metadata": "Writing title and tags…",
    "EmbedThumbnail": "Embedding cover art…",
    "EmbedSubtitle": "Adding subtitles…",
    "SubtitlesConvertor": "Converting subtitles…",
}
# Side files (subtitles) also trigger progress hooks; keep them out of the bar.
_SIDE_FILE_SUFFIXES = (".vtt", ".srt", ".ass", ".ttml", ".json3", ".srv1", ".srv2", ".srv3", ".jpg", ".webp", ".png")


class DownloadCancelled(Exception):
    pass


class _ProgressTracker:
    """Turns yt-dlp's per-file hook calls into one overall progress figure.

    A "1080p" download is really two downloads (video stream, then audio
    stream), each going 0->100%. We weight them by their expected sizes so the
    bar moves from 0 to 100 once.
    """

    def __init__(self, option: DownloadOption, emit: ProgressCallback, cancel: threading.Event | None):
        self.expected: list[int | None] = option.stream_sizes or [option.filesize]
        self.emit = emit
        self.cancel = cancel
        # filename -> (downloaded, total); dicts keep insertion order = stream order
        self.files: dict[str, tuple[int, int | None]] = {}
        self._last_emit = 0.0
        self._last_stage: str | None = None

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise DownloadCancelled()

    def _expected(self, i: int) -> int | None:
        return self.expected[i] if i < len(self.expected) else None

    def overall_percent(self) -> float | None:
        seen = list(self.files.values())
        n = max(len(self.expected), len(seen))
        done = totals = 0
        for i in range(n):
            dl, total = seen[i] if i < len(seen) else (0, None)
            total = total or self._expected(i)
            if not total:
                break
            done += min(dl, total)
            totals += total
        else:
            if totals:
                return 100.0 * done / totals
        # Some sizes unknown: give each stream an equal share of the bar.
        if not seen:
            return None
        dl, total = seen[-1]
        frac = (dl / total) if total else 0.0
        return 100.0 * (len(seen) - 1 + frac) / n

    def progress_hook(self, d: dict[str, Any]) -> None:
        self._check_cancel()
        status = d.get("status")
        filename = d.get("filename") or "?"
        if filename.lower().endswith(_SIDE_FILE_SUFFIXES):
            return
        total =d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes") or 0
        if status == "finished":
            size = int(total or downloaded)
            self.files[filename] = (size, size)
            return
        if status != "downloading":
            return
        self.files[filename] = (downloaded, int(total) if total else None)

        now = time.monotonic()
        if now - self._last_emit < 0.25:  # throttle to ~4 updates/sec
            return
        self._last_emit = now
        pct = self.overall_percent()
        self.emit(
            ProgressEvent(
                stage="downloading",
                percent=round(min(pct, 99.9), 1) if pct is not None else None,
                downloaded_bytes=sum(dl for dl, _ in self.files.values()),
                total_bytes=int(total) if total else None,
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        )

    def postprocessor_hook(self, d: dict[str, Any]) -> None:
        self._check_cancel()
        if d.get("status") != "started":
            return
        name = d.get("postprocessor", "")
        stage = _PP_STAGES.get(name)
        message = _PP_MESSAGES.get(name.removeprefix("FFmpeg"), "Finishing up…")
        if stage and message != self._last_stage:  # yt-dlp can report the same step twice
            self._last_stage = message
            self.emit(ProgressEvent(stage=stage, percent=100.0, message=message))


def _subtitle_opts(track_id: str) -> dict[str, Any]:
    """yt-dlp options to fetch one subtitle track ("en" or "auto:en-orig")."""
    auto = track_id.startswith("auto:")
    lang = track_id.removeprefix("auto:")
    return {
        "writesubtitles": not auto,
        "writeautomaticsub": auto,
        "subtitleslangs": [lang],
        "subtitlesformat": "srt/vtt/best",
    }


def build_ydl_opts(
    option: DownloadOption,
    out_dir: Path,
    *,
    subtitle: str | None = None,
    playlist_items: str | None = None,
    embed: bool | None = None,
) -> dict[str, Any]:
    """yt-dlp options for downloading `option` into `out_dir`.

    subtitle: track id to embed (video) or to save (the "srt" option).
    playlist_items: e.g. "3" to fetch only that item of an Instagram carousel.
    embed: write title/artist/chapters and the thumbnail into the file
           (defaults to EMBED_METADATA).
    """
    embed = EMBED_METADATA if embed is None else embed
    opts = base_ydl_opts() | {
        "paths": {"home": str(out_dir), "temp": str(out_dir)},
        # Title trimmed to 120 bytes; `windowsfilenames` strips characters that
        # are invalid on Windows (applied everywhere for portable names).
        "outtmpl": {"default": "%(title).120B [%(id)s].%(ext)s"},
        "windowsfilenames": True,
        "trim_file_name": 150,
        "overwrites": True,
        "continuedl": False,
    }
    if option.format:
        opts["format"] = option.format
    if option.format_sort:
        opts["format_sort"] = option.format_sort
    if playlist_items:
        opts["noplaylist"] = False
        opts["playlist_items"] = playlist_items

    # Order matters; it mirrors the order yt-dlp's CLI uses.
    postprocessors: list[dict[str, Any]] = []

    if option.kind == "subtitle":
        if not subtitle:
            raise ValueError("subtitle track required for the srt option")
        opts["skip_download"] = True
        opts.update(_subtitle_opts(subtitle))
        postprocessors.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
        opts["postprocessors"] = postprocessors
        return opts

    if embed:
        opts["writethumbnail"] = True
        # Thumbnails are often WebP, which MP3/MP4 cover art doesn't support.
        postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})

    if option.kind == "video":
        # MP4 when the codecs allow it, otherwise MKV.
        opts["merge_output_format"] = "mp4/mkv"
        postprocessors.append({"key": "FFmpegVideoRemuxer", "preferedformat": "mp4/mkv"})
        if subtitle:
            opts.update(_subtitle_opts(subtitle))
            postprocessors.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    elif option.ext == "mp3":
        postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"})
    # m4a: downloaded as-is, no re-encode.

    if embed:
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": option.kind == "video"})
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    opts["postprocessors"] = postprocessors
    return opts


def download(
    url: str,
    option: DownloadOption,
    out_dir: Path,
    on_progress: ProgressCallback,
    cancel: threading.Event | None = None,
    *,
    subtitle: str | None = None,
    playlist_items: str | None = None,
    embed: bool | None = None,
) -> Path:
    """Download `url` using `option` into `out_dir` and return the final file path.

    Raises UserFacingError on failure, DownloadCancelled if `cancel` was set.
    """
    require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    tracker = _ProgressTracker(option, on_progress, cancel)
    opts = build_ydl_opts(option, out_dir, subtitle=subtitle, playlist_items=playlist_items, embed=embed)
    opts["progress_hooks"] = [tracker.progress_hook]
    opts["postprocessor_hooks"] = [tracker.postprocessor_hook]

    on_progress(ProgressEvent(stage="downloading", percent=0.0, message="Starting…"))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise
    except Exception as exc:
        # yt-dlp may wrap exceptions raised inside our hooks.
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled() from exc
        raise friendly_error(exc) from exc

    if info and info.get("_type") == "playlist":  # Instagram carousel
        info = next((e for e in info.get("entries") or [] if e), info)
    downloads = (info or {}).get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") else None
    if path is None or not path.is_file():
        # Fallback (and the normal path for subtitle-only downloads): the
        # newest finished file in the job directory, preferring the target ext.
        files = [p for p in out_dir.iterdir() if p.is_file() and p.suffix not in (".part", ".ytdl")]
        wanted = [p for p in files if p.suffix.lower() == f".{option.ext}"]
        if not files:
            if option.kind == "subtitle":
                raise friendly_error(RuntimeError("Couldn't download subtitles for this video."))
            raise friendly_error(RuntimeError("Download finished but no output file was found."))
        path = max(wanted or files, key=lambda p: p.stat().st_mtime)

    on_progress(ProgressEvent(stage="done", percent=100.0, total_bytes=path.stat().st_size, message=path.name))
    return path
