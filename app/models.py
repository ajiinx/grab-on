"""Pydantic models shared by the extractor, downloader and API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["youtube", "instagram", "spotify", "twitter"]
OptionKind = Literal["video", "audio", "subtitle"]
MediaKind = Literal["video", "playlist"]

# Upper bound on how many playlist items one job may download.
MAX_PLAYLIST_ITEMS = 200


class DownloadOption(BaseModel):
    """One curated choice shown to the user, e.g. "1080p MP4" or "MP3"."""

    id: str  # stable id: best | 1080 | 720 | 480 | mp3 | m4a | srt
    label: str  # button text, e.g. "1080p"
    detail: str  # secondary text, e.g. "MP4 · 60fps"
    kind: OptionKind
    ext: str  # final container: mp4 | mkv | mp3 | m4a | srt
    # yt-dlp format selector. Concrete format ids first, generic fallback after,
    # so a download still works if ids shift between info and download.
    format: str
    # yt-dlp format sort (-S). Used for playlist options, where we can't see
    # each video's formats up front: e.g. ["res:1080", "vcodec:h264"].
    format_sort: list[str] | None = None
    filesize: int | None = None  # estimated total bytes
    # Estimated size of each stream yt-dlp will fetch, in download order
    # (video then audio). Used to compute one overall progress percentage.
    stream_sizes: list[int | None] = Field(default_factory=list)
    height: int | None = None  # short side in px (1080 for both 16:9 and 9:16)


class SpotifyTrack(BaseModel):
    """Song details read from Spotify; written into the downloaded file as tags."""

    id: str
    title: str
    artists: list[str]
    album: str | None = None
    duration: float | None = None  # seconds
    cover: str | None = None  # cover art URL (Spotify CDN)
    release_date: str | None = None  # "YYYY-MM-DD" or "YYYY"
    track_number: int | None = None

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


class TrackMatch(BaseModel):
    """The YouTube upload chosen as the audio source for a Spotify track."""

    url: str
    title: str
    channel: str | None = None
    duration: float | None = None


class PlaylistEntry(BaseModel):
    index: int  # 1-based position in the playlist (yt-dlp's playlist_index)
    id: str | None = None
    # Direct URL for the item (YouTube). None for Instagram carousel items,
    # which are downloaded as "post URL + playlist_items=index".
    url: str | None = None
    title: str
    duration: float | None = None
    thumbnail: str | None = None
    artist: str | None = None  # Spotify: display string, e.g. "Artist A, Artist B"
    spotify_id: str | None = None  # Spotify: track id, matched to YouTube at download time
    track_number: int | None = None  # Spotify albums: position on the album


class SubtitleTrack(BaseModel):
    id: str  # language code, prefixed "auto:" for auto-generated captions
    name: str  # display name, e.g. "English (auto-generated)"


class MediaInfo(BaseModel):
    platform: Platform
    kind: MediaKind = "video"
    url: str
    id: str
    title: str
    uploader: str | None = None
    duration: float | None = None  # seconds
    thumbnail: str | None = None
    options: list[DownloadOption]
    entries: list[PlaylistEntry] = Field(default_factory=list)  # kind == "playlist"
    subtitles: list[SubtitleTrack] = Field(default_factory=list)
    # Set when a single video was opened from a playlist link (watch?v=…&list=…).
    playlist_url: str | None = None
    # Spotify only: the song's details and (for single tracks) the YouTube match.
    spotify: SpotifyTrack | None = None
    match: TrackMatch | None = None
    album: str | None = None  # Spotify albums: album name for every entry


class ProgressEvent(BaseModel):
    stage: Literal["queued", "downloading", "merging", "converting", "processing", "done", "error", "cancelled"]
    percent: float | None = None  # 0-100, overall
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None  # bytes/sec
    eta: float | None = None  # seconds
    message: str | None = None
    filename: str | None = None  # set when stage == "done"
    error_code: str | None = None  # set when stage == "error"
    files: list[str] | None = None  # playlist jobs: names of the downloaded files


# --------------------------------------------------------------------------- #
# API request/response models
# --------------------------------------------------------------------------- #


class InfoRequest(BaseModel):
    url: str = Field(max_length=2048)


class DownloadRequest(BaseModel):
    url: str = Field(max_length=2048)
    option_id: str = Field(pattern=r"^[a-z0-9]{1,8}$")
    # Playlist item indices (1-based) to download; required for playlists.
    items: list[int] | None = Field(default=None, min_length=1, max_length=MAX_PLAYLIST_ITEMS)
    # Subtitle track id from MediaInfo.subtitles: embedded into video
    # downloads, or the language for the "srt" option.
    subtitle: str | None = Field(default=None, pattern=r"^(auto:)?[A-Za-z0-9_-]{1,40}$")


class JobCreated(BaseModel):
    job_id: str


class HistoryItem(BaseModel):
    id: int
    url: str
    platform: Platform
    title: str
    uploader: str | None = None
    thumbnail: str | None = None
    option_id: str
    option_label: str
    ext: str
    filesize: int | None = None
    item_count: int | None = None  # playlists: number of files downloaded
    created_at: str  # ISO 8601, UTC


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    yt_dlp: str
    yt_dlp_latest: str | None = None
    yt_dlp_update_available: bool = False
    ffmpeg: str | None = None
    ffmpeg_ok: bool
    cookies_configured: bool
    auth_enabled: bool
    max_concurrent_downloads: int
