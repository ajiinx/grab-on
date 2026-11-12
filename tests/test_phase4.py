"""Playlists, Instagram carousels, subtitles and ZIP streaming."""

import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main
from app.errors import UserFacingError
from app.extractor import (
    _instagram_post,
    _youtube_playlist,
    playlist_link,
    playlist_options,
    subtitle_tracks,
    validate_url,
)
from app.jobs import download_filename
from app.models import DownloadOption, MediaInfo, PlaylistEntry, SubtitleTrack
from app.storage import History, zip_stream

PL = "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"


# --------------------------------------------------------------------------- URLs


def test_playlist_url_accepted():
    assert validate_url("https://youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&si=x") == ("youtube", PL)


@pytest.mark.parametrize("raw", [
    "https://www.youtube.com/playlist?list=RDaqz-KE-bpKQ",  # endless mix
    "https://www.youtube.com/playlist?list=WL",  # watch later (too short + private)
    "https://www.youtube.com/playlist?list=PL<script>",
    "https://www.youtube.com/playlist",
])
def test_bad_playlist_urls_rejected(raw):
    with pytest.raises(UserFacingError):
        validate_url(raw)


def test_watch_link_inside_playlist():
    raw = "https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=3"
    assert validate_url(raw) == ("youtube", "https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    assert playlist_link(raw) == PL
    assert playlist_link("https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=RDaqz-KE-bpKQ") is None
    assert playlist_link("https://youtu.be/aqz-KE-bpKQ") is None


# --------------------------------------------------------------------------- extraction


def test_youtube_playlist_entries_skip_unavailable():
    raw = {
        "id": "PLx", "title": "My list", "uploader": "Me",
        "entries": [
            {"id": "aqz-KE-bpKQ", "title": "One", "duration": 60,
             "thumbnails": [{"url": "https://i.ytimg.com/s.jpg", "width": 168}, {"url": "https://i.ytimg.com/m.jpg", "width": 336}]},
            {"id": "jNQXAC9IVRw", "title": "[Private video]"},
            None,
            {"id": "tPEE9ZwTmy0", "title": "Three"},
        ],
    }
    info = _youtube_playlist(PL, raw)
    assert info.kind == "playlist" and info.title == "My list"
    assert [(e.index, e.title) for e in info.entries] == [(1, "One"), (4, "Three")]
    assert info.entries[0].url == "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    assert info.entries[0].thumbnail == "https://i.ytimg.com/m.jpg"
    assert [o.id for o in info.options] == ["best", "1080", "720", "480", "mp3", "m4a"]


def test_empty_playlist_is_an_error():
    with pytest.raises(UserFacingError):
        _youtube_playlist(PL, {"entries": [{"id": "x", "title": "[Deleted video]"}]})


def test_playlist_options_use_format_sort():
    opts = {o.id: o for o in playlist_options("youtube")}
    assert opts["720"].format_sort[0] == "res:720"
    assert opts["best"].format_sort[0] == "res"
    assert [o.id for o in playlist_options("instagram")] == ["best", "mp3"]


def _ig_video(i):
    return {"id": f"v{i}", "title": "Post", "duration": 5, "thumbnail": f"https://x.cdninstagram.com/{i}.jpg",
            "formats": [{"format_id": "dash-v", "url": "https://x/v", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
                         "width": 720, "height": 1280, "tbr": 1000},
                        {"format_id": "dash-a", "url": "https://x/a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 96}]}


def test_instagram_carousel_with_several_videos():
    image = {"id": "img", "formats": [{"format_id": "0", "url": "https://x/i.jpg", "ext": "jpg", "vcodec": "none", "acodec": "none"}]}
    raw = {"_type": "playlist", "id": "C1", "title": "Post by x", "entries": [_ig_video(1), image, _ig_video(3)]}
    info = _instagram_post("https://www.instagram.com/p/C1/", raw)
    assert info.kind == "playlist"
    assert [(e.index, e.title, e.url) for e in info.entries] == [(1, "Video 1", None), (3, "Video 2", None)]


def test_instagram_carousel_with_one_video_is_single():
    raw = {"_type": "playlist", "id": "C1", "entries": [None, _ig_video(2)]}
    info = _instagram_post("https://www.instagram.com/p/C1/", raw)
    assert info.kind == "video"
    assert [o.id for o in info.options] == ["best", "mp3", "m4a"]
    assert info.entries[0].index == 2  # so the download fetches only item 2


def test_subtitle_tracks():
    info = {
        "subtitles": {
            "de": [{"ext": "vtt", "name": "German"}],
            "en-GB": [{"ext": "vtt", "name": "English (United Kingdom)"}],
            "live_chat": [{"ext": "json"}],
        },
        "automatic_captions": {
            "en-orig": [{"name": "English (Original)"}],  # covered by en-GB manual track
            "fr-orig": [{"name": "French (Original)"}],
            "fr": [{"name": "French"}],  # machine translation — skipped
            "es": [{"name": "Spanish"}],
        },
    }
    tracks = subtitle_tracks(info)
    assert [(t.id, t.name) for t in tracks] == [
        ("en-GB", "English (United Kingdom)"),
        ("de", "German"),
        ("auto:fr-orig", "French (auto-generated)"),
    ]
    assert subtitle_tracks({}) == []


def test_srt_filename_keeps_language():
    assert download_filename(Path("My Talk [aqz-KE-bpKQ].en.srt")) == "My Talk.en.srt"
    assert download_filename(Path("My Talk [aqz-KE-bpKQ].pt-BR.srt")) == "My Talk.pt-BR.srt"


# --------------------------------------------------------------------------- storage


def test_zip_stream_roundtrip(tmp_path):
    a = tmp_path / "a.mp4"
    a.write_bytes(b"A" * 3_000_000)
    b = tmp_path / "b.mp3"
    b.write_bytes(b"B" * 10)
    data = b"".join(zip_stream([("01 - a.mp4", a), ("02 - b.mp3", b)], chunk_size=1 << 18))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist() == ["01 - a.mp4", "02 - b.mp3"]
        assert zf.read("01 - a.mp4") == b"A" * 3_000_000
        assert zf.testzip() is None


def test_history_migrates_old_schema(tmp_path):
    import sqlite3

    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, platform TEXT NOT NULL,"
                 " title TEXT NOT NULL, uploader TEXT, thumbnail TEXT, option_id TEXT NOT NULL, option_label TEXT NOT NULL,"
                 " ext TEXT NOT NULL, filesize INTEGER, created_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    h = History(db)
    item = h.add(url="u", platform="youtube", title="t", uploader=None, thumbnail=None, option_id="mp3",
                 option_label="MP3", ext="zip", filesize=1, item_count=3)
    assert item.item_count == 3
    h.close()


# --------------------------------------------------------------------------- API


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
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["stage"] in ("done", "error", "cancelled"):
            return state
        time.sleep(0.05)
    raise AssertionError(state)


def test_playlist_job_zips_selected_items_and_skips_failures(client, monkeypatch):
    info = MediaInfo(
        platform="youtube", kind="playlist", url=PL, id="PLx", title="My: list", options=playlist_options("youtube"),
        entries=[PlaylistEntry(index=i, id=f"id{i}", url=f"https://www.youtube.com/watch?v=vid{i:08d}", title=f"T{i}")
                 for i in (1, 2, 3, 4)],
    )
    monkeypatch.setattr(main, "get_media_info", lambda url: info)
    calls = []

    def fake_download(url, option, out_dir, on_progress, cancel=None, **kwargs):
        calls.append((url, option.id, kwargs.get("playlist_items")))
        if url.endswith("3"):
            raise Exception("ERROR: [youtube] x: Video unavailable")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"Song {url[-1]} [abcdefghijk].mp3"
        path.write_bytes(url[-1].encode() * 100)
        return path

    monkeypatch.setattr(jobs, "download", fake_download)
    r = client.post("/api/download", json={"url": PL, "option_id": "mp3", "items": [4, 1, 3]})
    state = _wait(client, r.json()["job_id"])
    assert state["stage"] == "done", state
    assert [c[0][-1] for c in calls] == ["1", "3", "4"]  # playlist order, not request order
    assert state["files"] == ["01 - Song 1.mp3", "03 - Song 4.mp3"]
    assert state["message"] == "2 of 3 downloaded · 1 failed"
    assert state["filename"] == "My list.zip"

    job_id = r.json()["job_id"]
    z = client.get(f"/api/file/{job_id}")
    assert z.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert zf.namelist() == ["01 - Song 1.mp3", "03 - Song 4.mp3"]
    one = client.get(f"/api/file/{job_id}/2")
    assert one.status_code == 200 and one.content == b"4" * 100
    assert client.get(f"/api/file/{job_id}/3").status_code == 404

    hist = client.get("/api/history").json()[0]
    assert hist["item_count"] == 2 and hist["ext"] == "zip"


def test_playlist_job_rejects_unknown_items(client, monkeypatch):
    info = MediaInfo(platform="youtube", kind="playlist", url=PL, id="x", title="t", options=playlist_options("youtube"),
                     entries=[PlaylistEntry(index=1, url="https://www.youtube.com/watch?v=aqz-KE-bpKQ", title="a")])
    monkeypatch.setattr(main, "get_media_info", lambda url: info)
    r = client.post("/api/download", json={"url": PL, "option_id": "mp3", "items": [7]})
    assert _wait(client, r.json()["job_id"])["error_code"] == "bad_items"


def test_carousel_item_and_subtitles_passed_to_downloader(client, monkeypatch):
    url = "https://www.instagram.com/p/C1/"
    single = MediaInfo(
        platform="youtube", url=url, id="x", title="t",
        options=[DownloadOption(id="720", label="720p", detail="", kind="video", ext="mp4", format="22"),
                 DownloadOption(id="srt", label="Subtitles", detail="", kind="subtitle", ext="srt", format="")],
        entries=[PlaylistEntry(index=2, title="t")],
        subtitles=[SubtitleTrack(id="en", name="English"), SubtitleTrack(id="auto:de-orig", name="German (auto)")],
    )
    monkeypatch.setattr(main, "get_media_info", lambda u: single)
    seen = {}

    def fake_download(u, option, out_dir, on_progress, cancel=None, **kwargs):
        seen[option.id] = kwargs
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / ("t [abcdefghijk].en.srt" if option.id == "srt" else "t [abcdefghijk].mp4")
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr(jobs, "download", fake_download)
    j = client.post("/api/download", json={"url": url, "option_id": "720", "subtitle": "auto:de-orig"}).json()["job_id"]
    assert _wait(client, j)["stage"] == "done"
    assert seen["720"] == {"subtitle": "auto:de-orig", "playlist_items": "2"}

    j = client.post("/api/download", json={"url": url, "option_id": "srt"}).json()["job_id"]
    state = _wait(client, j)
    assert seen["srt"]["subtitle"] == "en"  # default track
    assert state["filename"] == "t.en.srt"

    j = client.post("/api/download", json={"url": url, "option_id": "720", "subtitle": "xx"}).json()["job_id"]
    assert _wait(client, j)["error_code"] == "bad_subtitle"


def test_download_request_validation(client):
    assert client.post("/api/download", json={"url": PL, "option_id": "mp3", "items": []}).status_code == 422
    assert client.post("/api/download", json={"url": PL, "option_id": "mp3", "items": list(range(1, 300))}).status_code == 422
    assert client.post("/api/download", json={"url": PL, "option_id": "mp3", "subtitle": "en;rm"}).status_code == 422


def test_playlist_link_not_lost_by_info_cache(client, monkeypatch):
    video = MediaInfo(platform="youtube", url="https://www.youtube.com/watch?v=aqz-KE-bpKQ", id="x", title="t",
                      options=[DownloadOption(id="mp3", label="MP3", detail="", kind="audio", ext="mp3", format="ba")])
    monkeypatch.setattr(main, "get_media_info", lambda u: video)
    plain = client.post("/api/info", json={"url": "https://youtu.be/aqz-KE-bpKQ"}).json()
    assert plain["playlist_url"] is None
    linked = client.post("/api/info", json={"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"}).json()
    assert linked["playlist_url"] == PL
