"""yt-dlp wrapper: URL validation, metadata extraction and option curation.

The raw yt-dlp format list for a YouTube video has 40+ entries. `build_options`
reduces it to a handful of clean choices (Best / 1080p / 720p / 480p / MP3 /
M4A), listing only the ones that actually exist for that video.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yt_dlp

from . import spotify
from .config import base_ydl_opts
from .errors import UserFacingError, friendly_error
from .models import MAX_PLAYLIST_ITEMS, DownloadOption, MediaInfo, Platform, PlaylistEntry, SubtitleTrack

# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
SPOTIFY_HOSTS = {"open.spotify.com"}
SPOTIFY_SHORT_HOSTS = {"spotify.link", "spotify.app.link"}
TWITTER_HOSTS = {
    "twitter.com",
    "www.twitter.com",
    "m.twitter.com",
    "mobile.twitter.com",
    "x.com",
    "www.x.com",
    "m.x.com",
    "mobile.x.com",
}

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_LIST = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
# Auto-generated "mixes" (RD...) are endless radio lists; LL/WL need login.
_YT_LIST_EXCLUDED = ("RD", "UL", "LL", "WL")
_YT_PATH = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})/?$")
# Optional username prefix: instagram.com/<user>/reel/<code>/ also exists.
_IG_PATH = re.compile(r"^/(?:[A-Za-z0-9_.]+/)?(p|reel|reels|tv)/([A-Za-z0-9_-]+)/?$")

# Localised links look like /intl-de/track/<id> or /intl-es-419/track/<id>.
_SP_PATH = re.compile(r"^/(?:intl-[a-zA-Z0-9_-]+/)?(track|album|playlist)/([A-Za-z0-9]{22})/?$")

# User or i/web prefix: twitter.com/<user>/status/<id>, x.com/<user>/status/<id>/video/1
_TWITTER_PATH = re.compile(r"^/(?:([A-Za-z0-9_]+)/status(?:es)?|i/(?:web/)?status)/([0-9]+)(?:/(?:video|photo)/([0-9]+))?/?$")

_INVALID = "That doesn't look like a YouTube, Instagram, Spotify or Twitter/X link."


def validate_url(raw: str) -> tuple[Platform, str]:
    """Check a user-supplied URL against the allow-list.

    Returns (platform, canonical_url). Raises UserFacingError if the URL is
    not an https YouTube video/short/playlist or Instagram reel/post.
    """
    s = (raw or "").strip()
    if not s or len(s) > 2048 or any(c.isspace() for c in s):
        raise UserFacingError("invalid_url", _INVALID)
    if "://" not in s:
        s = "https://" + s  # allow pasting "youtu.be/abc..." without a scheme

    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        raise UserFacingError("invalid_url", _INVALID) from None
    if parts.scheme.lower() != "https":
        raise UserFacingError("invalid_url", "Only https:// links are supported.")
    if parts.username or parts.password or port not in (None, 443):
        raise UserFacingError("invalid_url", _INVALID)

    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path

    if host in YOUTU_BE_HOSTS:
        video_id = path.strip("/")
        if _YT_ID.match(video_id):
            return "youtube", f"https://www.youtube.com/watch?v={video_id}"
    elif host in YOUTUBE_HOSTS:
        if path.rstrip("/") == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
            if _YT_ID.match(video_id):
                return "youtube", f"https://www.youtube.com/watch?v={video_id}"
        elif path.rstrip("/") == "/playlist":
            list_id = parse_qs(parts.query).get("list", [""])[0]
            if _YT_LIST.match(list_id) and not list_id.startswith(_YT_LIST_EXCLUDED):
                return "youtube", f"https://www.youtube.com/playlist?list={list_id}"
        elif m := _YT_PATH.match(path):
            kind = path.split("/")[1]
            if kind == "shorts":
                return "youtube", f"https://www.youtube.com/shorts/{m.group(1)}"
            return "youtube", f"https://www.youtube.com/watch?v={m.group(1)}"
    elif host in INSTAGRAM_HOSTS:
        if m := _IG_PATH.match(path):
            kind = "reel" if m.group(1) in ("reel", "reels") else m.group(1)
            return "instagram", f"https://www.instagram.com/{kind}/{m.group(2)}/"
    elif host in SPOTIFY_HOSTS:
        if m := _SP_PATH.match(path):
            return "spotify", f"https://open.spotify.com/{m.group(1)}/{m.group(2)}"
    elif host in SPOTIFY_SHORT_HOSTS:
        raise UserFacingError(
            "invalid_url",
            "Short spotify.link links aren't supported. Open it, then copy the full open.spotify.com link.",
        )
    elif host in TWITTER_HOSTS:
        if m := _TWITTER_PATH.match(path):
            user = m.group(1) or "i"
            tw_id = m.group(2)
            v_idx = m.group(3)
            canonical = f"https://x.com/{user}/status/{tw_id}"
            if v_idx:
                canonical += f"/video/{v_idx}"
            return "twitter", canonical
    else:
        raise UserFacingError("unsupported_site", "Only YouTube, Instagram, Spotify and Twitter/X links are supported.")

    raise UserFacingError("invalid_url", _INVALID)


def is_playlist_url(url: str) -> bool:
    """True for canonical YouTube playlist URLs (as returned by validate_url)."""
    return url.startswith("https://www.youtube.com/playlist?list=")


def playlist_link(raw: str) -> str | None:
    """For a watch?v=...&list=... link, the canonical URL of that playlist."""
    raw = raw.strip()
    try:
        parts = urlsplit(raw if "://" in raw else "https://" + raw)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in YOUTUBE_HOSTS:
        return None
    list_id = parse_qs(parts.query).get("list", [""])[0]
    if _YT_LIST.match(list_id) and not list_id.startswith(_YT_LIST_EXCLUDED):
        return f"https://www.youtube.com/playlist?list={list_id}"
    return None



# --------------------------------------------------------------------------- #
# Format helpers
# --------------------------------------------------------------------------- #

# Bitrate yt-dlp's "--audio-quality 0" (LAME V0) typically lands on, in kbps.
MP3_V0_KBPS = 245

# (option id, lower bound exclusive, upper bound inclusive) on the short side.
VIDEO_TIERS: list[tuple[str, int, int]] = [
    ("1080", 720, 1080),
    ("720", 480, 720),
    ("480", 360, 480),
]


def _has_video(f: dict) -> bool:
    vcodec = f.get("vcodec")
    if vcodec == "none":
        return False
    return vcodec is not None or bool(f.get("height") or f.get("width"))


def _has_audio(f: dict) -> bool:
    # acodec None means "unknown" — progressive files (e.g. Instagram) usually have audio.
    return f.get("acodec") != "none"


def _is_usable(f: dict) -> bool:
    if f.get("has_drm") or f.get("ext") == "mhtml":  # storyboards
        return False
    if not f.get("url") and not f.get("fragments") and not f.get("manifest_url"):
        return False
    # YouTube's "DRC" (dynamic range compressed) audio tracks are a worse duplicate.
    fid = str(f.get("format_id", ""))
    if "drc" in fid.lower() or "drc" in str(f.get("format_note", "")).lower():
        return False
    return _has_video(f) or _has_audio(f)


def short_side(f: dict) -> int | None:
    """Resolution as people name it: 1080 for both 1920x1080 and 1080x1920."""
    w, h = f.get("width"), f.get("height")
    if w and h:
        return min(w, h)
    return h or None


def _is_portrait(f: dict) -> bool:
    w, h = f.get("width"), f.get("height")
    return bool(w and h and h > w)


def _codec_rank(vcodec: str | None) -> int:
    """Prefer codecs with the widest playback support (phones, Windows players)."""
    v = (vcodec or "").lower()
    if v.startswith(("avc", "h264")):
        return 3
    if v.startswith(("vp9", "vp09")):
        return 2
    if v.startswith(("av01", "av1")):
        return 1
    return 0


def _proto_rank(f: dict) -> int:
    # Plain https downloads are faster and more reliable than HLS fragments.
    return 1 if str(f.get("protocol", "https")).startswith("http") else 0


def _codec_name(vcodec: str | None) -> str:
    return {3: "H.264", 2: "VP9", 1: "AV1"}.get(_codec_rank(vcodec), "")


def estimate_size(f: dict, duration: float | None) -> int | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return int(size)
    tbr = f.get("tbr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def _video_key(f: dict) -> tuple:
    return (short_side(f) or 0, f.get("fps") or 0, _codec_rank(f.get("vcodec")), _proto_rank(f), f.get("tbr") or 0)


def _audio_key(f: dict, prefer_m4a: bool) -> tuple:
    is_m4a = f.get("ext") == "m4a" or str(f.get("acodec", "")).startswith("mp4a")
    lang = f.get("language_preference")
    return (
        # YouTube marks the original-language track with a high value, dubs
        # with -1; "unknown" (None) ranks with the dubs.
        lang if lang is not None else -1,
        # HLS audio on YouTube lacks bitrate/size info; prefer plain https.
        _proto_rank(f),
        int(is_m4a) if prefer_m4a else 0,
        f.get("abr") or f.get("tbr") or 0,
    )


def _sum_sizes(sizes: list[int | None]) -> int | None:
    return None if any(s is None for s in sizes) else sum(s for s in sizes if s is not None)


def _fmt_list(info: dict) -> list[dict]:
    formats = info.get("formats")
    if not formats:
        # Some extractors return a single format inline in the info dict.
        formats = [info] if info.get("url") else []
    return [f for f in formats if _is_usable(f)]


# --------------------------------------------------------------------------- #
# Option curation
# --------------------------------------------------------------------------- #


def _video_option(
    option_id: str,
    label: str,
    video: dict,
    audio: dict | None,
    duration: float | None,
) -> DownloadOption:
    side = short_side(video) or 0
    dim = "width" if _is_portrait(video) else "height"
    vid = video["format_id"]
    progressive = _has_audio(video) and video.get("acodec") is not None
    if progressive or audio is None:
        # Progressive file (already has audio), or no separate audio exists.
        fmt = f"{vid}/b[{dim}<={side}]/b"
        streams = [estimate_size(video, duration)]
    else:
        fmt = f"{vid}+{audio['format_id']}/bv*[{dim}<={side}]+ba/b[{dim}<={side}]/b"
        streams = [estimate_size(video, duration), estimate_size(audio, duration)]

    bits = ["MP4"]
    fps = video.get("fps")
    if fps and fps > 30:
        bits.append(f"{round(fps)} fps")
    if codec := _codec_name(video.get("vcodec")):
        bits.append(codec)

    return DownloadOption(
        id=option_id,
        label=label,
        detail=" · ".join(bits),
        kind="video",
        ext="mp4",
        format=fmt,
        filesize=_sum_sizes(streams),
        stream_sizes=streams,
        height=side or None,
    )


def _best_label(side: int) -> str:
    names = {4320: "8K", 2160: "4K", 1440: "2K"}
    return f"{side}p" + (f" · {names[side]}" if side in names else "")


def build_options(info: dict, platform: Platform) -> list[DownloadOption]:
    """Reduce yt-dlp's raw format list to a few curated download options."""
    duration = info.get("duration")
    formats = _fmt_list(info)

    videos = [f for f in formats if _has_video(f)]
    audio_only = [f for f in formats if _has_audio(f) and not _has_video(f)]

    best_audio_any = max(audio_only, key=lambda f: _audio_key(f, prefer_m4a=False), default=None)
    # AAC/m4a audio for MP4 output keeps files playable everywhere.
    best_audio_mp4 = max(audio_only, key=lambda f: _audio_key(f, prefer_m4a=True), default=None)

    options: list[DownloadOption] = []

    if videos:
        top = max(videos, key=_video_key)
        top_side = short_side(top) or 0

        if platform == "instagram":
            opt = _video_option("best", "Video", top, best_audio_mp4, duration)
            if top_side:
                opt.detail = f"MP4 · {top_side}p"
            options.append(opt)
        else:
            tier_opts: list[DownloadOption] = []
            for tier_id, low, high in VIDEO_TIERS:
                in_tier = [f for f in videos if low < (short_side(f) or 0) <= high]
                if in_tier:
                    best = max(in_tier, key=_video_key)
                    tier_opts.append(_video_option(tier_id, f"{tier_id}p", best, best_audio_mp4, duration))

            # "Best" only when it adds something: above 1080p, or when no tier
            # matched at all (e.g. a 360p-only upload).
            if top_side > 1080 or not tier_opts:
                label = _best_label(top_side) if top_side else "Best quality"
                best = _video_option("best", label, top, best_audio_mp4, duration)
                best.detail = "Best · " + best.detail
                options.append(best)
            options.extend(tier_opts)

    has_any_audio = bool(audio_only) or any(_has_audio(f) for f in videos)
    if has_any_audio:
        src = best_audio_any
        options.append(
            DownloadOption(
                id="mp3",
                label="MP3",
                detail=f"Audio · ~{MP3_V0_KBPS} kbps",
                kind="audio",
                ext="mp3",
                format=f"{src['format_id']}/ba/b" if src else "ba/b",
                filesize=int(MP3_V0_KBPS * 1000 / 8 * duration) if duration else None,
                stream_sizes=[estimate_size(src, duration) if src else None],
            )
        )

    m4a = [f for f in audio_only if f.get("ext") == "m4a"]
    if m4a:
        src = max(m4a, key=lambda f: _audio_key(f, prefer_m4a=True))
        abr = src.get("abr") or src.get("tbr")
        size = estimate_size(src, duration)
        options.append(
            DownloadOption(
                id="m4a",
                label="M4A",
                detail="Original audio" + (f" · {round(abr)} kbps" if abr else ""),
                kind="audio",
                ext="m4a",
                format=f"{src['format_id']}/ba[ext=m4a]/ba",
                filesize=size,
                stream_sizes=[size],
            )
        )

    return options


# --------------------------------------------------------------------------- #
# Playlist options and subtitles
# --------------------------------------------------------------------------- #

# Sort order mirroring _video_key: resolution, fps, then H.264 + AAC for
# compatibility, then plain https over HLS. yt-dlp's "res" is the smaller
# dimension, so vertical videos are handled like in build_options.
_SORT_TAIL = ["fps", "vcodec:h264", "acodec:aac", "proto:https"]


def playlist_options(platform: Platform) -> list[DownloadOption]:
    """Options for a playlist. Each video's formats are unknown until it's
    downloaded, so these use format sorting ("up to 1080p") instead of ids."""

    def video(option_id: str, label: str, detail: str, res: str | None) -> DownloadOption:
        sort = ([f"res:{res}"] if res else ["res"]) + _SORT_TAIL
        return DownloadOption(
            id=option_id, label=label, detail=detail, kind="video", ext="mp4", format="bv*+ba/b", format_sort=sort
        )

    mp3 = DownloadOption(
        id="mp3", label="MP3", detail=f"Audio · ~{MP3_V0_KBPS} kbps", kind="audio", ext="mp3", format="ba/b"
    )
    if platform == "instagram":
        return [video("best", "Video", "MP4 · best available", None), mp3]
    if platform == "twitter":
        return [
            video("best", "Best", "Highest available · MP4", None),
            video("1080", "1080p", "Or lower if unavailable", "1080"),
            video("720", "720p", "Or lower if unavailable", "720"),
            video("480", "480p", "Or lower if unavailable", "480"),
            mp3,
        ]
    return [
        video("best", "Best", "Highest available · MP4", None),
        video("1080", "1080p", "Or lower if unavailable", "1080"),
        video("720", "720p", "Or lower if unavailable", "720"),
        video("480", "480p", "Or lower if unavailable", "480"),
        mp3,
        DownloadOption(id="m4a", label="M4A", detail="Original audio", kind="audio", ext="m4a", format="ba[ext=m4a]/ba/b"),
    ]


MAX_SUBTITLE_TRACKS = 60


def subtitle_tracks(info: dict) -> list[SubtitleTrack]:
    """Uploaded subtitles, plus YouTube's auto-generated captions in the
    video's original language (not the 100+ machine translations)."""
    tracks: list[SubtitleTrack] = []
    for lang, fmts in (info.get("subtitles") or {}).items():
        if lang == "live_chat" or not fmts:
            continue
        tracks.append(SubtitleTrack(id=lang, name=fmts[0].get("name") or lang))
    manual = {t.id.split("-")[0] for t in tracks}
    for lang, fmts in (info.get("automatic_captions") or {}).items():
        if not lang.endswith("-orig") or not fmts:
            continue
        base = lang.removesuffix("-orig")
        if base.split("-")[0] in manual:
            continue  # a human-made track exists in this language
        name = (fmts[0].get("name") or base).replace(" (Original)", "")
        tracks.append(SubtitleTrack(id=f"auto:{lang}", name=f"{name} (auto-generated)"))
    # Human-made first, English first within each group, then alphabetical.
    tracks.sort(key=lambda t: (t.id.startswith("auto:"), not t.id.removeprefix("auto:").startswith("en"), t.name.lower()))
    return tracks[:MAX_SUBTITLE_TRACKS]


def subtitle_option() -> DownloadOption:
    return DownloadOption(id="srt", label="Subtitles", detail="SRT file", kind="subtitle", ext="srt", format="")


def default_subtitle(tracks: list[SubtitleTrack]) -> str | None:
    """Track the SRT option uses when none was picked (the list is English-first)."""
    return tracks[0].id if tracks else None


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def _check_live(info: dict) -> None:
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise UserFacingError("live", "Live streams and upcoming premieres can't be downloaded until they've finished.")


def _extract(url: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    opts = base_ydl_opts() | {"skip_download": True} | (extra or {})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises DownloadError/ExtractorError and friends
        raise friendly_error(exc) from exc
    if not info:
        raise UserFacingError("unsupported", "Couldn't read that link.")
    return info


def fetch_raw_info(url: str) -> dict[str, Any]:
    """Metadata for a single video or Instagram post (no download). URL must be pre-validated."""
    return _extract(url)


def fetch_playlist_info(url: str) -> dict[str, Any]:
    """Fast, flat listing of a YouTube playlist: titles and ids, no formats."""
    return _extract(url, {"noplaylist": False, "extract_flat": "in_playlist", "playlistend": MAX_PLAYLIST_ITEMS})


def _thumb_of(entry: dict) -> str | None:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = [t for t in entry.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return None
    # Flat YouTube entries list several sizes, smallest first; take a medium one.
    sized = [t for t in thumbs if (t.get("width") or 0) >= 320]
    return (sized[0] if sized else thumbs[-1])["url"]


_UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]", "[Unavailable video]"}


def _youtube_playlist(url: str, raw: dict) -> MediaInfo:
    entries: list[PlaylistEntry] = []
    for i, e in enumerate(raw.get("entries") or [], start=1):
        if not e or e.get("title") in _UNAVAILABLE_TITLES or not _YT_ID.match(str(e.get("id") or "")):
            continue
        entries.append(
            PlaylistEntry(
                index=i,
                id=e["id"],
                url=f"https://www.youtube.com/watch?v={e['id']}",
                title=e.get("title") or e["id"],
                duration=e.get("duration"),
                thumbnail=_thumb_of(e),
            )
        )
    if not entries:
        raise UserFacingError("unsupported", "This playlist is empty, private, or all of its videos are unavailable.")
    return MediaInfo(
        platform="youtube",
        kind="playlist",
        url=url,
        id=str(raw.get("id", "")),
        title=raw.get("title") or "Playlist",
        uploader=raw.get("uploader") or raw.get("channel"),
        thumbnail=entries[0].thumbnail,
        options=playlist_options("youtube"),
        entries=entries,
    )


def _single(platform: Platform, url: str, info: dict, raw_url: str) -> MediaInfo:
    _check_live(info)
    options = build_options(info, platform)
    if not options:
        raise UserFacingError("unsupported", "No downloadable formats were found for this link.")
    subs = subtitle_tracks(info)
    if subs:
        options.append(subtitle_option())
    return MediaInfo(
        platform=platform,
        url=url,
        id=str(info.get("id", "")),
        title=info.get("title") or info.get("id") or "Untitled",
        uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        options=options,
        subtitles=subs,
        playlist_url=playlist_link(raw_url) if platform == "youtube" else None,
    )


def _multi_video_post(platform: Platform, url: str, raw: dict) -> MediaInfo:
    """Carousels and multi-video posts come back as a playlist. With one video we treat the post as
    a single video; with several the user picks which ones to download."""
    videos = [
        (i, e)
        for i, e in enumerate(raw.get("entries") or [], start=1)
        if e and any(_has_video(f) for f in _fmt_list(e))
    ]
    if not videos:
        raise UserFacingError("unsupported", "Couldn't find a video in that post.")
    if len(videos) == 1:
        index, entry = videos[0]
        info = _single(platform, url, entry, url)
        # Remember which carousel/post item it is, so the download fetches only that one.
        info.entries = [PlaylistEntry(index=index, title=info.title, thumbnail=info.thumbnail)]
        return info
    entries = [
        PlaylistEntry(
            index=i,
            id=str(e.get("id") or i),
            title=f"Video {n}",
            duration=e.get("duration"),
            thumbnail=_thumb_of(e),
        )
        for n, (i, e) in enumerate(videos, start=1)
    ]
    default_title = "Instagram post" if platform == "instagram" else "Twitter post"
    return MediaInfo(
        platform=platform,
        kind="playlist",
        url=url,
        id=str(raw.get("id", "")),
        title=raw.get("title") or default_title,
        uploader=raw.get("uploader") or raw.get("channel"),
        thumbnail=entries[0].thumbnail,
        options=playlist_options(platform),
        entries=entries,
    )


def _instagram_post(url: str, raw: dict) -> MediaInfo:
    return _multi_video_post("instagram", url, raw)


def _spotify_audio_options(options: list[DownloadOption]) -> list[DownloadOption]:
    """Audio options with honest labels: the source is YouTube audio
    (~130-160 kbps), so MP3 is a re-encode and M4A is the untouched original."""
    out = []
    for o in options:
        if o.id == "mp3":
            o.detail = "Plays everywhere"
        elif o.id == "m4a":
            o.detail = "Best quality · no re-encode"
        out.append(o)
    return out


def _spotify_info(url: str) -> MediaInfo:
    kind, spotify_id = url.rstrip("/").split("/")[-2:]
    if kind == "track":
        track = spotify.get_track(spotify_id)
        match = spotify.find_match(track)
        raw = fetch_raw_info(match.url)
        options = _spotify_audio_options([o for o in build_options(raw, "youtube") if o.kind == "audio"])
        if not options:
            raise UserFacingError("unsupported", "The YouTube match for this song has no downloadable audio.")
        return MediaInfo(
            platform="spotify",
            url=url,
            id=spotify_id,
            title=track.title,
            uploader=track.artist,
            duration=track.duration,
            thumbnail=track.cover,
            options=options,
            spotify=track,
            match=match,
        )
    coll = spotify.get_collection(kind, spotify_id)
    if not coll["entries"]:
        raise UserFacingError("unsupported", f"This {kind} has no playable tracks.")
    return MediaInfo(
        platform="spotify",
        kind="playlist",
        url=url,
        id=spotify_id,
        title=coll["title"],
        uploader=coll["owner"],
        thumbnail=coll["cover"],
        options=_spotify_audio_options([o for o in playlist_options("youtube") if o.kind == "audio"]),
        entries=coll["entries"],
        album=coll["title"] if kind == "album" else None,
    )


def get_media_info(raw_url: str) -> MediaInfo:
    platform, url = validate_url(raw_url)
    if platform == "spotify":
        return _spotify_info(url)
    if is_playlist_url(url):
        return _youtube_playlist(url, fetch_playlist_info(url))
    raw = fetch_raw_info(url)
    if raw.get("_type") == "playlist":
        return _multi_video_post(platform, url, raw)
    return _single(platform, url, raw, raw_url)
