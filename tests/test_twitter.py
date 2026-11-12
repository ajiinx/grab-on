"""Tests for Twitter/X integration: URLs, format curation, playlists, error handling, API."""

from pathlib import Path
import time
import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main
from app.errors import UserFacingError, friendly_error
from app.extractor import (
    build_options,
    get_media_info,
    playlist_options,
    validate_url,
)
from app.models import DownloadOption, MediaInfo, PlaylistEntry, ProgressEvent

TWITTER_URL = "https://x.com/jack/status/20"


# --------------------------------------------------------------------------- URLs


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://x.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://twitter.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://www.twitter.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://www.x.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://m.twitter.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://mobile.x.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://x.com/jack/status/20?s=20&t=abcdef", ("twitter", "https://x.com/jack/status/20")),
        ("x.com/jack/status/20", ("twitter", "https://x.com/jack/status/20")),
        ("https://twitter.com/jack/statuses/20/", ("twitter", "https://x.com/jack/status/20")),
        ("https://x.com/i/web/status/1850123456789012345", ("twitter", "https://x.com/i/status/1850123456789012345")),
        ("https://twitter.com/i/status/1850123456789012345", ("twitter", "https://x.com/i/status/1850123456789012345")),
        ("https://x.com/user_1/status/1850123456789012345/video/1", ("twitter", "https://x.com/user_1/status/1850123456789012345/video/1")),
        ("https://twitter.com/user_1/status/1850123456789012345/photo/2/", ("twitter", "https://x.com/user_1/status/1850123456789012345/video/2")),
    ],
)
def test_twitter_valid_urls(raw, expected):
    assert validate_url(raw) == expected


@pytest.mark.parametrize(
    "raw, code",
    [
        ("https://x.com", "invalid_url"),
        ("https://x.com/", "invalid_url"),
        ("https://x.com/jack", "invalid_url"),  # profile
        ("https://twitter.com/elonmusk", "invalid_url"),
        ("https://x.com/home", "invalid_url"),
        ("https://x.com/explore", "invalid_url"),
        ("https://twitter.com/i/bookmarks", "invalid_url"),
        ("https://x.com/jack/status/", "invalid_url"),
        ("https://x.com/jack/status/notdigits", "invalid_url"),
        ("http://x.com/jack/status/20", "invalid_url"),  # http not allowed
        ("https://x.com.evil.com/jack/status/20", "unsupported_site"),
    ],
)
def test_twitter_invalid_urls(raw, code):
    with pytest.raises(UserFacingError) as exc:
        validate_url(raw)
    assert exc.value.code == code


# --------------------------------------------------------------------------- format curation


def _tw_fmt(fid, w, h, tbr=1000, acodec="mp4a.40.2", size=None):
    return {
        "format_id": fid,
        "url": f"https://video.twimg.com/{fid}.mp4",
        "ext": "mp4",
        "width": w,
        "height": h,
        "vcodec": "avc1.4d401f",
        "acodec": acodec,
        "fps": 30,
        "tbr": tbr,
        "filesize": size,
        "protocol": "https",
    }


def test_twitter_tiered_options():
    info = {
        "id": "1850123456789012345",
        "title": "A cool video",
        "duration": 20,
        "formats": [
            _tw_fmt("http-2176", 1920, 1080, tbr=2176, size=5_000_000),
            _tw_fmt("http-832", 1280, 720, tbr=832, size=2_000_000),
            _tw_fmt("http-288", 852, 480, tbr=288, size=800_000),
        ],
    }
    opts = build_options(info, "twitter")
    assert [o.id for o in opts] == ["1080", "720", "480", "mp3"]
    assert opts[0].label == "1080p"
    assert opts[0].ext == "mp4"
    assert opts[0].filesize == 5_000_000
    assert opts[1].label == "720p"
    assert opts[2].label == "480p"
    assert opts[3].id == "mp3"


def test_twitter_vertical_video():
    info = {
        "id": "1850123456789012345",
        "duration": 15,
        "formats": [
            _tw_fmt("http-832", 720, 1280, tbr=832, size=1_500_000),
        ],
    }
    opts = build_options(info, "twitter")
    assert [o.id for o in opts] == ["720", "mp3"]
    assert opts[0].height == 720
    assert "[width<=720]" in opts[0].format


def test_twitter_low_res_fallback_to_best():
    info = {
        "id": "1850123456789012345",
        "duration": 10,
        "formats": [
            _tw_fmt("http-128", 480, 270, tbr=128),
        ],
    }
    opts = build_options(info, "twitter")
    assert opts[0].id == "best"
    assert opts[0].label == "270p"


def test_twitter_silent_video():
    info = {
        "id": "1850123456789012345",
        "duration": 10,
        "formats": [
            _tw_fmt("http-832", 1280, 720, acodec="none"),
        ],
    }
    opts = build_options(info, "twitter")
    assert [o.id for o in opts] == ["720"]


# --------------------------------------------------------------------------- multi-video tweets


def test_twitter_multi_video_post():
    image = {"id": "img1", "formats": [{"format_id": "0", "url": "https://pbs.twimg.com/media/i.jpg", "vcodec": "none", "acodec": "none"}]}
    v1 = {"id": "v1", "duration": 10, "thumbnail": "https://pbs.twimg.com/thumb1.jpg", "formats": [_tw_fmt("h1", 1280, 720)]}
    v2 = {"id": "v2", "duration": 20, "thumbnail": "https://pbs.twimg.com/thumb2.jpg", "formats": [_tw_fmt("h2", 1920, 1080)]}
    raw = {"_type": "playlist", "id": "123", "title": "Multi-video tweet", "uploader": "Alice", "entries": [v1, image, v2]}

    info = get_media_info.__wrapped__("https://x.com/alice/status/123") if hasattr(get_media_info, "__wrapped__") else None

    # Call _multi_video_post directly to test behavior
    from app.extractor import _multi_video_post
    media = _multi_video_post("twitter", "https://x.com/alice/status/123", raw)
    assert media.platform == "twitter"
    assert media.kind == "playlist"
    assert len(media.entries) == 2
    assert [(e.index, e.title) for e in media.entries] == [(1, "Video 1"), (3, "Video 2")]
    assert [o.id for o in media.options] == ["best", "1080", "720", "480", "mp3"]


def test_twitter_post_without_video_raises_unsupported():
    from app.extractor import _multi_video_post
    image = {"id": "img1", "formats": [{"format_id": "0", "url": "https://pbs.twimg.com/media/i.jpg", "vcodec": "none", "acodec": "none"}]}
    raw = {"_type": "playlist", "id": "123", "entries": [image]}
    with pytest.raises(UserFacingError) as exc:
        _multi_video_post("twitter", "https://x.com/alice/status/123", raw)
    assert exc.value.code == "unsupported"


# --------------------------------------------------------------------------- error handling


@pytest.mark.parametrize(
    "raw_msg, expected_code",
    [
        ("ERROR: [twitter] 1850: These tweets are protected. Only approved followers can see them.", "private"),
        ("ERROR: [twitter] 1850: Protected tweet", "private"),
        ("ERROR: [twitter] 1850: Tweet not found", "unavailable"),
        ("ERROR: [twitter] 1850: This tweet is unavailable.", "unavailable"),
        ("ERROR: [twitter] 1850: Tweet has been deleted.", "unavailable"),
        ("ERROR: [twitter] 1850: User has been suspended.", "unavailable"),
        ("ERROR: [twitter] 1850: No video could be found in this tweet", "unsupported"),
        ("ERROR: [twitter] 1850: Unable to download webpage: timed out", "network"),
    ],
)
def test_twitter_error_translation(raw_msg, expected_code):
    err = friendly_error(Exception(raw_msg))
    assert err.code == expected_code


# --------------------------------------------------------------------------- API integration


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(config, "APP_PASSWORD", None)
    monkeypatch.setattr(main, "info_limiter", main.RateLimiter(100))
    monkeypatch.setattr(main, "download_limiter", main.RateLimiter(100))
    monkeypatch.setattr(main, "info_cache", main.InfoCache())
    monkeypatch.setattr(main.updates, "latest_version", lambda timeout=5.0: None)
    with TestClient(main.app) as c:
        yield c


def fake_twitter_info(url=TWITTER_URL):
    return MediaInfo(
        platform="twitter",
        url=url,
        id="20",
        title="just setting up my twttr",
        uploader="jack",
        duration=15,
        thumbnail="https://pbs.twimg.com/media/test.jpg",
        options=[
            DownloadOption(id="720", label="720p", detail="MP4 · H.264", kind="video", ext="mp4", format="http-832", filesize=2000),
            DownloadOption(id="mp3", label="MP3", detail="Audio", kind="audio", ext="mp3", format="ba/b", filesize=500),
        ],
    )


def test_twitter_api_info_and_download(client, monkeypatch):
    monkeypatch.setattr(main, "get_media_info", lambda url: fake_twitter_info(url))

    # Test /api/info
    res = client.post("/api/info", json={"url": "https://twitter.com/jack/status/20"})
    assert res.status_code == 200
    data = res.json()
    assert data["platform"] == "twitter"
    assert data["title"] == "just setting up my twttr"
    assert data["uploader"] == "jack"
    assert len(data["options"]) == 2

    # Test /api/download
    def fake_download(url, option, out_dir: Path, on_progress, cancel=None, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        on_progress(ProgressEvent(stage="downloading", percent=50.0))
        path = out_dir / "just setting up my twttr [1850123456789012345].mp4"
        path.write_bytes(b"v" * 2000)
        return path

    monkeypatch.setattr(jobs, "download", fake_download)

    res = client.post("/api/download", json={"url": "https://x.com/jack/status/20", "option_id": "720"})
    assert res.status_code == 200
    job_id = res.json()["job_id"]

    for _ in range(50):
        st = client.get(f"/api/jobs/{job_id}").json()
        if st["stage"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st["stage"] == "done"
    assert st["filename"] == "just setting up my twttr.mp4"

    # Verify download file
    file_res = client.get(f"/api/file/{job_id}")
    assert file_res.status_code == 200
    assert len(file_res.content) == 2000

    # Verify history
    history = client.get("/api/history").json()
    assert len(history) == 1
    assert history[0]["platform"] == "twitter"
    assert history[0]["option_id"] == "720"


def test_twitter_thumbnail_proxy_allow_list(client):
    from app.main import _thumb_allowed

    assert _thumb_allowed("https://pbs.twimg.com/media/abc.jpg")
    assert _thumb_allowed("https://video.twimg.com/ext_tw_video_thumb/123/pu/img/abc.jpg")
    assert _thumb_allowed("https://abs.twimg.com/responsive-web/client-web/icon.png")
    assert _thumb_allowed("https://twitter.com/thumb.jpg")
    assert _thumb_allowed("https://x.com/thumb.jpg")
    assert not _thumb_allowed("https://evil-twimg.com/test.jpg")
    assert not _thumb_allowed("http://pbs.twimg.com/test.jpg")
