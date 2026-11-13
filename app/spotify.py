"""Spotify support: read song details from Spotify, download audio from YouTube.

Spotify streams are DRM-protected, so nothing is downloaded from Spotify. We
read public metadata (title, artists, duration, cover) from Spotify's embed
page — no API key needed — find the same recording on YouTube, download that,
and write Spotify's metadata and cover art into the file.

The embed page is an undocumented, public endpoint; if Spotify changes it,
only `_embed_entity` needs updating.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import socket
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yt_dlp

from .config import base_ydl_opts
from .errors import UserFacingError, friendly_error
from .models import DownloadOption, PlaylistEntry, ProgressEvent, SpotifyTrack, TrackMatch

log = logging.getLogger(__name__)

SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_INITIAL_STATE = re.compile(r'<script id="initial-state"[^>]*>(.*?)</script>', re.S)
COVER_HOSTS = ("spotifycdn.com", "scdn.co")
_MAX_COVER_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Reading Spotify
# --------------------------------------------------------------------------- #


def _http_get(url: str, limit: int = 4 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # fixed https Spotify URLs only
        return resp.read(limit)


def _embed_entity(kind: str, spotify_id: str) -> dict[str, Any]:
    """The JSON behind open.spotify.com/embed/<kind>/<id>."""
    if kind not in ("track", "album", "playlist") or not SPOTIFY_ID.match(spotify_id):
        raise UserFacingError("invalid_url", "That doesn't look like a Spotify link.")
    try:
        html = _http_get(f"https://open.spotify.com/embed/{kind}/{spotify_id}").decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            raise UserFacingError("unavailable", f"Couldn't find that {kind} on Spotify. It may be private or removed.") from exc
        raise friendly_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UserFacingError("network", "Couldn't reach Spotify. Check your connection and try again.") from exc

    entity = None
    m = _NEXT_DATA.search(html)
    if m:
        try:
            entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        except (KeyError, TypeError, json.JSONDecodeError):
            entity = None

    if not entity:
        m2 = _INITIAL_STATE.search(html)
        if m2:
            raw = m2.group(1).strip()
            data = None
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    data = json.loads(base64.b64decode(raw).decode("utf-8"))
                except Exception:
                    data = None
            if isinstance(data, dict):
                entity = data.get("entity") or data.get("data", {}).get("entity")

    if not entity:
        raise UserFacingError("unavailable", f"Couldn't read that {kind} from Spotify. It may be private, or Spotify changed its page.")
    return entity


def _cover_of(entity: dict) -> str | None:
    images = (entity.get("visualIdentity") or {}).get("image") or []
    images = [i for i in images if i.get("url")]
    if images:
        return max(images, key=lambda i: i.get("maxWidth") or 0)["url"]
    sources = (entity.get("coverArt") or {}).get("sources") or []
    return max(sources, key=lambda s: s.get("width") or 0)["url"] if sources else None


def _release(entity: dict) -> str | None:
    iso = (entity.get("releaseDate") or {}).get("isoString")
    return iso[:10] if iso else None


def get_track(spotify_id: str) -> SpotifyTrack:
    e = _embed_entity("track", spotify_id)
    artists = [a["name"] for a in e.get("artists") or [] if a.get("name")]
    return SpotifyTrack(
        id=spotify_id,
        title=e.get("title") or e.get("name") or "Unknown",
        artists=artists or ["Unknown artist"],
        duration=(e["duration"] / 1000) if e.get("duration") else None,
        cover=_cover_of(e),
        release_date=_release(e),
    )


def get_collection(kind: str, spotify_id: str) -> dict[str, Any]:
    """Album or playlist: title, owner/artist, cover and its tracks.

    Spotify's embed lists up to ~100 tracks of a playlist.
    """
    e = _embed_entity(kind, spotify_id)
    cover = _cover_of(e)
    entries: list[PlaylistEntry] = []
    for i, t in enumerate(e.get("trackList") or [], start=1):
        uri = t.get("uri") or ""
        track_id = uri.rsplit(":", 1)[-1]
        if not uri.startswith("spotify:track:") or not SPOTIFY_ID.match(track_id):
            continue  # podcast episodes, local files
        if t.get("isPlayable") is False:
            continue
        entries.append(
            PlaylistEntry(
                index=i,
                id=track_id,
                spotify_id=track_id,
                title=t.get("title") or "Unknown",
                artist=(t.get("subtitle") or "").replace("\xa0", " ") or None,  # Spotify uses NBSPs
                duration=(t["duration"] / 1000) if t.get("duration") else None,
                thumbnail=cover if kind == "album" else None,
                track_number=i if kind == "album" else None,
            )
        )
    return {
        "title": e.get("title") or e.get("name") or kind.title(),
        "owner": e.get("subtitle"),
        "cover": cover,
        "release_date": _release(e),
        "entries": entries,
    }


def track_for_entry(entry: PlaylistEntry, album: str | None, album_cover: str | None) -> SpotifyTrack:
    """Full details for a playlist/album entry (artists list, cover), falling
    back to what the listing already had if the track page can't be read."""
    try:
        track = get_track(entry.spotify_id or "")
    except UserFacingError:
        track = SpotifyTrack(
            id=entry.spotify_id or "",
            title=entry.title,
            artists=[entry.artist] if entry.artist else ["Unknown artist"],
            duration=entry.duration,
            cover=album_cover,
        )
    track.album = album or track.album
    track.track_number = entry.track_number
    track.cover = track.cover or album_cover
    return track


# --------------------------------------------------------------------------- #
# Matching on YouTube
# --------------------------------------------------------------------------- #

# Versions that are almost never what a Spotify link means, unless the
# Spotify title itself says so.
_BAD_WORDS = (
    "live", "cover", "remix", "karaoke", "instrumental", "8d", "slowed", "sped up", "speed up", "reverb",
    "nightcore", "acoustic", "piano", "reaction", "tutorial", "lesson", "extended", "mashup", "bass boosted",
    "loop", "1 hour", "10 hours", "concert", "rehearsal", "demo", "lyrics", "perform", "performs", "performance",
    "tiktok", "full album", "making of", "behind the scenes", "interview", "trailer", "teaser", "snippet", "preview",
)
SEARCH_RESULTS = 10


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _primary_title(title: str) -> str:
    """'Song (feat. X) - 2011 Remaster' -> 'Song'."""
    return re.split(r"\s+-\s+|\s*[(\[]", title, maxsplit=1)[0].strip() or title


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def score_candidate(track: SpotifyTrack, cand: dict) -> float | None:
    """Score a YouTube search result for `track`; None means "reject"."""
    title = _norm(cand.get("title") or "")
    channel = _norm(cand.get("channel") or cand.get("uploader") or "")
    spotify_title = _norm(track.title)
    duration = cand.get("duration")

    score = 100.0
    if track.duration and duration:
        diff = abs(duration - track.duration)
        if diff > max(15.0, 0.1 * track.duration):
            return None  # different edit, a live version, a 1-hour loop…
        score -= diff * 2
    else:
        score -= 15

    primary = _norm(_primary_title(track.title))
    if primary and primary not in title:
        score -= 35
    score += 20 * SequenceMatcher(None, spotify_title, title).ratio()

    artists = [_norm(a) for a in track.artists if a]
    channel_name = channel.removesuffix(" topic").removesuffix(" vevo").replace(" ", "")
    if any(a and a.replace(" ", "") in channel_name for a in artists) or channel.endswith(" vevo"):
        score += 25  # the artist's own channel: the official upload
    elif any(a and a in title for a in artists):
        score += 5  # a re-upload that names the artist
    else:
        score -= 20
    if channel.endswith(" topic"):
        score += 15  # YouTube's auto-generated "Artist - Topic" uploads are the album audio
    for word in _BAD_WORDS:
        if _has_word(title, word) and not _has_word(spotify_title, word):
            score -= 40
    if "official audio" in title or _has_word(title, "audio"):
        score += 5
    if "official video" in title or "music video" in title:
        score -= 3  # music videos often have intros/skits
    return score


def search_youtube(query: str, limit: int = SEARCH_RESULTS) -> list[dict]:
    opts = base_ydl_opts() | {"skip_download": True, "extract_flat": "in_playlist", "noplaylist": False}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    except Exception as exc:  # noqa: BLE001
        raise friendly_error(exc) from exc
    return [e for e in (result or {}).get("entries") or [] if e and e.get("id")]


def find_match(track: SpotifyTrack) -> TrackMatch:
    """Best YouTube upload for a Spotify track.

    "official audio" steers the search towards the artist's own audio upload
    (no video intro, album version); the plain query is the fallback.
    """
    clean_artist = re.sub(r"[\r\n\t]+", " ", track.artists[0]).strip()
    clean_title = re.sub(r"[\r\n\t]+", " ", track.title).strip()
    base = f"{clean_artist} - {clean_title}"
    scored: list[tuple[float, dict]] = []
    for query in (f"{base} official audio", base):
        scored = [(s, c) for c in search_youtube(query) if (s := score_candidate(track, c)) is not None]
        if scored:
            break
    if not scored:
        raise UserFacingError("no_match", f"Couldn't find \"{track.artist} - {track.title}\" on YouTube.")
    best_score, best = max(scored, key=lambda sc: sc[0])
    log.info("Spotify match for %s - %s: %s (%s, score %.0f)", track.artist, track.title, best.get("title"), best.get("id"), best_score)
    return TrackMatch(
        url=f"https://www.youtube.com/watch?v={best['id']}",
        title=best.get("title") or best["id"],
        channel=best.get("channel") or best.get("uploader"),
        duration=best.get("duration"),
    )


# --------------------------------------------------------------------------- #
# Tagging and downloading
# --------------------------------------------------------------------------- #


def _is_safe_ip(host: str) -> bool:
    try:
        # Check if already an IP
        try:
            ip = ipaddress.ip_address(host)
            return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
        except ValueError:
            pass
        addr_info = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        for addr in addr_info:
            ip = ipaddress.ip_address(addr[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return False
        return bool(addr_info)
    except (socket.gaierror, ValueError, OSError):
        return False


def _cover_allowed(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and parts.port in (None, 443)
        and not parts.username
        and any(host == h or host.endswith("." + h) for h in COVER_HOSTS)
    )


class _CoverRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parts = urllib.parse.urlsplit(newurl)
            host = (parts.hostname or "").lower()
            if not _cover_allowed(newurl) or not _is_safe_ip(host):
                return None
        except Exception:
            return None
        return urllib.request.Request(newurl, headers=req.headers)


_cover_opener = urllib.request.build_opener(_CoverRedirectHandler)


def fetch_cover(url: str | None) -> bytes | None:
    if not url or not _cover_allowed(url):
        return None
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not _is_safe_ip(host):
            return None
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
        with _cover_opener.open(req, timeout=15) as resp:
            data = resp.read(_MAX_COVER_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return data if len(data) <= _MAX_COVER_BYTES else None


def _cover_format(cover: bytes | None) -> tuple[str, int] | None:
    if not cover or len(cover) < 4:
        return None
    from mutagen.mp4 import MP4Cover
    if cover.startswith(b"\x89PNG"):
        return "image/png", MP4Cover.FORMAT_PNG
    return "image/jpeg", MP4Cover.FORMAT_JPEG


def write_tags(path: Path, track: SpotifyTrack, cover: bytes | None) -> None:
    """Write Spotify's title/artist/album/date/track number and cover into MP3 or M4A."""
    ext = path.suffix.lower()
    img_info = _cover_format(cover)
    if ext == ".mp3":
        from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError

        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(TIT2(encoding=3, text=track.title))
        tags.add(TPE1(encoding=3, text=track.artist))  # "A, B" — v2.3 would join a list as "A/B"
        tags.add(TPE2(encoding=3, text=track.artists[0]))
        if track.album:
            tags.add(TALB(encoding=3, text=track.album))
        if track.release_date:
            tags.add(TDRC(encoding=3, text=track.release_date))
        if track.track_number:
            tags.add(TRCK(encoding=3, text=str(track.track_number)))
        if cover:
            mime = img_info[0] if img_info else "image/jpeg"
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=3)  # ID3v2.3 is what Windows Explorer and most players read best
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4Cover

        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        tags["\xa9nam"] = [track.title]
        tags["\xa9ART"] = [track.artist]
        tags["aART"] = [track.artists[0]]
        if track.album:
            tags["\xa9alb"] = [track.album]
        if track.release_date:
            tags["\xa9day"] = [track.release_date]
        if track.track_number:
            tags["trkn"] = [(track.track_number, 0)]
        if cover:
            img_fmt = img_info[1] if img_info else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(cover, imageformat=img_fmt)]
        audio.save()
    # Other containers (rare fallbacks like .webm/.opus) keep YouTube's tags.


def nice_filename(track: SpotifyTrack, suffix: str) -> str:
    name = f"{track.artist} - {track.title}"
    name = re.sub(r'[\x00-\x1f\x7f"\\/:*?<>|]+', "", name).strip(" .")[:150].rstrip(" .")
    return f"{name or 'track'}{suffix}"


def download_track(
    track: SpotifyTrack,
    option: DownloadOption,
    out_dir: Path,
    on_progress,
    cancel: threading.Event | None = None,
    match: TrackMatch | None = None,
) -> Path:
    """Find (unless given), download and tag one Spotify track. Returns the file path."""
    from .downloader import download  # local import: downloader is heavier and unused at import time

    if match is None:
        on_progress(ProgressEvent(stage="downloading", percent=0.0, message="Finding it on YouTube…"))
        match = find_match(track)
    path = download(match.url, option, out_dir, on_progress, cancel, embed=False)
    on_progress(ProgressEvent(stage="processing", percent=100.0, message="Adding Spotify tags and cover…"))
    try:
        write_tags(path, track, fetch_cover(track.cover))
    except Exception:  # noqa: BLE001 — a tagging problem shouldn't lose the download
        log.exception("Couldn't tag %s", path)
    final = path.with_name(nice_filename(track, path.suffix))
    if final != path:
        path = path.replace(final)
    return path
