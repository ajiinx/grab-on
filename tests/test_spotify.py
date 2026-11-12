"""Spotify: URL validation, metadata parsing, YouTube matching, tagging, jobs."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main, spotify
from app.errors import UserFacingError
from app.extractor import validate_url
from app.models import MediaInfo, PlaylistEntry, SpotifyTrack, TrackMatch

TRACK = "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8"


# --------------------------------------------------------------------------- URLs


@pytest.mark.parametrize("raw, expected", [
    ("https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8?si=abc123", TRACK),
    ("open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8", TRACK),
    ("https://open.spotify.com/intl-de/track/4PTG3Z6ehGkBFwjybzWkR8", TRACK),
    ("https://open.spotify.com/intl-pt-BR/track/4PTG3Z6ehGkBFwjybzWkR8", TRACK),
    ("https://open.spotify.com/album/6XhjNHCyCDyyGJRM5mg40G", "https://open.spotify.com/album/6XhjNHCyCDyyGJRM5mg40G"),
    ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M/", "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"),
])
def test_spotify_urls(raw, expected):
    assert validate_url(raw) == ("spotify", expected)


@pytest.mark.parametrize("raw", [
    "https://open.spotify.com/artist/0gxyHStUsqpMadRV0Di1Qt",  # artists aren't downloadable lists
    "https://open.spotify.com/episode/4PTG3Z6ehGkBFwjybzWkR8",  # podcasts
    "https://open.spotify.com/track/short",
    "http://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8",
    "https://spotify.link/AbCdEf",
    "https://open.spotify.com.evil.com/track/4PTG3Z6ehGkBFwjybzWkR8",
])
def test_bad_spotify_urls(raw):
    with pytest.raises(UserFacingError):
        validate_url(raw)


# --------------------------------------------------------------------------- metadata


def test_get_track_parses_embed(monkeypatch):
    monkeypatch.setattr(spotify, "_embed_entity", lambda kind, sid: {
        "title": "Song", "artists": [{"name": "A"}, {"name": "Tyler, The Creator"}], "duration": 201500,
        "releaseDate": {"isoString": "2020-05-01T00:00:00Z"},
        "visualIdentity": {"image": [{"url": "https://i.scdn.co/image/s", "maxWidth": 64},
                                     {"url": "https://i.scdn.co/image/l", "maxWidth": 640}]},
    })
    t = spotify.get_track("4PTG3Z6ehGkBFwjybzWkR8")
    assert t.artists == ["A", "Tyler, The Creator"] and t.artist == "A, Tyler, The Creator"
    assert t.duration == 201.5 and t.release_date == "2020-05-01" and t.cover == "https://i.scdn.co/image/l"


def test_get_collection_skips_episodes_and_unplayable(monkeypatch):
    monkeypatch.setattr(spotify, "_embed_entity", lambda kind, sid: {
        "title": "Album", "subtitle": "Band",
        "visualIdentity": {"image": [{"url": "https://i.scdn.co/image/c", "maxWidth": 300}]},
        "trackList": [
            {"uri": "spotify:track:7GhIk7Il098yCjg4BQjzvb", "title": "One", "subtitle": "Band", "duration": 1000},
            {"uri": "spotify:episode:5qUAdDl59w0Vbu4Gi6ecSX", "title": "Podcast"},
            {"uri": "spotify:track:5qUAdDl59w0Vbu4Gi6ecSX", "title": "Gone", "isPlayable": False},
            {"uri": "spotify:track:3zOYmiNOuyDL0ClxJcEJ0T", "title": "Four", "subtitle": "Band, Guest", "duration": 2000},
        ],
    })
    c = spotify.get_collection("album", "6XhjNHCyCDyyGJRM5mg40G")
    assert c["title"] == "Album" and c["owner"] == "Band"
    assert [(e.index, e.title, e.track_number, e.thumbnail) for e in c["entries"]] == [
        (1, "One", 1, "https://i.scdn.co/image/c"),
        (4, "Four", 4, "https://i.scdn.co/image/c"),
    ]


def test_track_for_entry_falls_back_to_listing(monkeypatch):
    def boom(sid):
        raise UserFacingError("unavailable", "x")

    monkeypatch.setattr(spotify, "get_track", boom)
    e = PlaylistEntry(index=3, spotify_id="7GhIk7Il098yCjg4BQjzvb", title="T", artist="A", duration=10, track_number=3)
    t = spotify.track_for_entry(e, "Album", "https://i.scdn.co/image/c")
    assert (t.title, t.artists, t.album, t.track_number, t.cover) == ("T", ["A"], "Album", 3, "https://i.scdn.co/image/c")


# --------------------------------------------------------------------------- matching

SONG = SpotifyTrack(id="x", title="Blinding Lights", artists=["The Weeknd"], duration=200)


def cand(title, channel, duration):
    return {"id": title[:11].ljust(11, "x"), "title": title, "channel": channel, "duration": duration}


def test_prefers_official_upload_over_reupload():
    official = spotify.score_candidate(SONG, cand("The Weeknd - Blinding Lights (Official Audio)", "The Weeknd", 202))
    reupload = spotify.score_candidate(SONG, cand("The Weeknd - Blinding Lights", "LatinHype", 200))
    topic = spotify.score_candidate(SONG, cand("Blinding Lights", "The Weeknd - Topic", 200))
    assert topic > reupload and official > reupload


@pytest.mark.parametrize("title", [
    "The Weeknd - Blinding Lights (Live at the Super Bowl)",
    "Blinding Lights (Slowed + Reverb)",
    "Blinding Lights - Piano Cover",
    "The Weeknd performs Blinding Lights",
    "Blinding Lights (Remix)",
])
def test_penalises_other_versions(title):
    good = spotify.score_candidate(SONG, cand("Blinding Lights", "The Weeknd - Topic", 200))
    bad = spotify.score_candidate(SONG, cand(title, "The Weeknd", 200))
    assert bad < good - 30


def test_remix_allowed_when_spotify_title_is_a_remix():
    remix = SpotifyTrack(id="x", title="Blinding Lights (Chromatics Remix)", artists=["The Weeknd"], duration=300)
    assert spotify.score_candidate(remix, cand("Blinding Lights (Chromatics Remix)", "The Weeknd", 300)) > 120


def test_rejects_wrong_duration():
    assert spotify.score_candidate(SONG, cand("Blinding Lights 1 hour", "x", 3600)) is None
    assert spotify.score_candidate(SONG, cand("Blinding Lights extended", "The Weeknd", 260)) is None


def test_find_match_falls_back_to_plain_query(monkeypatch):
    queries = []

    def search(q, limit=10):
        queries.append(q)
        return [] if "official audio" in q else [cand("Blinding Lights", "The Weeknd - Topic", 200)]

    monkeypatch.setattr(spotify, "search_youtube", search)
    m = spotify.find_match(SONG)
    assert queries == ["The Weeknd - Blinding Lights official audio", "The Weeknd - Blinding Lights"]
    assert m.channel == "The Weeknd - Topic" and m.url.startswith("https://www.youtube.com/watch?v=")


def test_find_match_none(monkeypatch):
    monkeypatch.setattr(spotify, "search_youtube", lambda q, limit=10: [cand("Something else", "x", 999)])
    with pytest.raises(UserFacingError) as e:
        spotify.find_match(SONG)
    assert e.value.code == "no_match"


def test_cover_host_allow_list():
    assert spotify._cover_allowed("https://i.scdn.co/image/abc")
    assert spotify._cover_allowed("https://image-cdn-ak.spotifycdn.com/image/abc")
    assert not spotify._cover_allowed("https://evil.com/scdn.co")
    assert not spotify._cover_allowed("http://i.scdn.co/image/abc")
    assert spotify.fetch_cover("https://169.254.169.254/latest") is None


def test_nice_filename():
    t = SpotifyTrack(id="x", title='What/Is "This"?', artists=["AC/DC", "B"])
    assert spotify.nice_filename(t, ".mp3") == "ACDC, B - WhatIs This.mp3"


# --------------------------------------------------------------------------- tagging (needs ffmpeg)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c"
    "20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100"
    "000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114"
    "328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a63646566"
    "6768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3"
    "d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)
TAGGED = SpotifyTrack(id="x", title="Song", artists=["A", "B"], album="Album", release_date="2020-05-01", track_number=7)


def _make(tmp_path: Path, ext: str) -> Path:
    out = tmp_path / f"t.{ext}"
    codec = ["-c:a", "libmp3lame"] if ext == "mp3" else ["-c:a", "aac"]
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1", *codec, str(out)], check=True)
    return out


@needs_ffmpeg
def test_write_tags_mp3(tmp_path):
    from mutagen.id3 import ID3

    p = _make(tmp_path, "mp3")
    spotify.write_tags(p, TAGGED, JPEG)
    tags = ID3(p)
    assert str(tags["TIT2"]) == "Song" and str(tags["TPE1"]) == "A, B"
    assert str(tags["TALB"]) == "Album" and str(tags["TRCK"]) == "7"
    assert tags.getall("APIC")[0].data == JPEG


@needs_ffmpeg
def test_write_tags_m4a(tmp_path):
    from mutagen.mp4 import MP4

    p = _make(tmp_path, "m4a")
    spotify.write_tags(p, TAGGED, JPEG)
    tags = MP4(p).tags
    assert tags["\xa9nam"] == ["Song"] and tags["\xa9ART"] == ["A, B"] and tags["\xa9alb"] == ["Album"]
    assert tags["trkn"] == [(7, 0)] and bytes(tags["covr"][0]) == JPEG


# --------------------------------------------------------------------------- jobs


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(config, "APP_PASSWORD", None)
    monkeypatch.setattr(main, "info_cache", main.InfoCache())
    monkeypatch.setattr(main, "download_limiter", main.RateLimiter(100))
    monkeypatch.setattr(main.updates, "latest_version", lambda timeout=5.0: None)
    with TestClient(main.app) as c:
        yield c


def _wait(client, job_id):
    for _ in range(100):
        st = client.get(f"/api/jobs/{job_id}").json()
        if st["stage"] in ("done", "error", "cancelled"):
            return st
        time.sleep(0.05)
    raise AssertionError(st)


def _audio_opts():
    from app.extractor import _spotify_audio_options, playlist_options

    return _spotify_audio_options([o for o in playlist_options("youtube") if o.kind == "audio"])


def test_spotify_track_job_uses_known_match(client, monkeypatch):
    track = SpotifyTrack(id="4PTG3Z6ehGkBFwjybzWkR8", title="Song", artists=["A"], cover="https://i.scdn.co/image/c")
    match = TrackMatch(url="https://www.youtube.com/watch?v=aqz-KE-bpKQ", title="A - Song", channel="A")
    info = MediaInfo(platform="spotify", url=TRACK, id=track.id, title="Song", uploader="A", options=_audio_opts(),
                     spotify=track, match=match)
    monkeypatch.setattr(main, "get_media_info", lambda u: info)
    seen = {}

    def fake(track, option, out_dir, on_progress, cancel=None, match=None):
        seen.update(track=track.title, match=match.url if match else None, option=option.id)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "A - Song.m4a"
        p.write_bytes(b"x" * 10)
        return p

    monkeypatch.setattr(jobs.spotify, "download_track", fake)
    j = client.post("/api/download", json={"url": TRACK, "option_id": "m4a"}).json()["job_id"]
    st = _wait(client, j)
    assert st["stage"] == "done" and st["filename"] == "A - Song.m4a"
    assert seen == {"track": "Song", "match": match.url, "option": "m4a"}
    assert client.get("/api/history").json()[0]["platform"] == "spotify"


def test_spotify_album_job(client, monkeypatch):
    url = "https://open.spotify.com/album/6XhjNHCyCDyyGJRM5mg40G"
    entries = [PlaylistEntry(index=i, spotify_id=f"{i:022d}", title=f"T{i}", artist="A", track_number=i) for i in (1, 2)]
    info = MediaInfo(platform="spotify", kind="playlist", url=url, id="x", title="Album", options=_audio_opts(),
                     entries=entries, album="Album", thumbnail="https://i.scdn.co/image/c")
    monkeypatch.setattr(main, "get_media_info", lambda u: info)
    monkeypatch.setattr(jobs.spotify, "track_for_entry",
                        lambda e, album, cover: SpotifyTrack(id=e.spotify_id, title=e.title, artists=["A"], album=album, cover=cover))

    def fake(track, option, out_dir, on_progress, cancel=None, match=None):
        assert match is None and track.album == "Album" and track.cover == "https://i.scdn.co/image/c"
        if track.title == "T2":
            raise UserFacingError("no_match", "Couldn't find it on YouTube.")
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"A - {track.title}.mp3"
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr(jobs.spotify, "download_track", fake)
    j = client.post("/api/download", json={"url": url, "option_id": "mp3"}).json()["job_id"]
    st = _wait(client, j)
    assert st["stage"] == "done" and st["files"] == ["01 - A - T1.mp3"]
    assert st["message"] == "1 of 2 downloaded · 1 failed"


def test_spotify_options_are_audio_only():
    assert [(o.id, o.kind) for o in _audio_opts()] == [("mp3", "audio"), ("m4a", "audio")]
