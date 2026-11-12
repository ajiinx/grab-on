"""API tests using FastAPI's TestClient. yt-dlp is stubbed out — no network needed."""

import base64
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main
from app.jobs import download_filename
from app.models import DownloadOption, MediaInfo, ProgressEvent

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "HISTORY_DB", tmp_path / "history.db")
    monkeypatch.setattr(config, "APP_PASSWORD", None)
    monkeypatch.setattr(main, "info_limiter", main.RateLimiter(100))
    monkeypatch.setattr(main, "download_limiter", main.RateLimiter(100))
    monkeypatch.setattr(main, "info_cache", main.InfoCache())
    monkeypatch.setattr(main.updates, "latest_version", lambda timeout=5.0: None)  # no network
    with TestClient(main.app) as c:
        yield c


def fake_info(url=URL):
    return MediaInfo(
        platform="youtube", url=url, id="aqz-KE-bpKQ", title="Test: video/<1>", uploader="Blender",
        duration=10, thumbnail="https://i.ytimg.com/vi/x/hq.jpg",
        options=[DownloadOption(id="720", label="720p", detail="MP4", kind="video", ext="mp4", format="22", filesize=10)],
    )


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["yt_dlp"] and "ffmpeg_ok" in body and body["auth_enabled"] is False


def test_index_served_with_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Grab</title>" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_auth_required_when_password_set(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret")
    r = client.get("/api/health")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    bad = base64.b64encode(b"me:wrong").decode()
    assert client.get("/api/health", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"anyone:s3cret").decode()
    assert client.get("/api/health", headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert client.get("/", headers={"Authorization": "Basic !!notbase64"}).status_code == 401


def test_info_rejects_other_sites(client):
    r = client.post("/api/info", json={"url": "https://vimeo.com/1"})
    assert r.status_code == 400
    assert r.json() == {"error": "unsupported_site", "message": "Only YouTube, Instagram, Spotify and Twitter/X links are supported."}


def test_info_hides_internal_fields(client, monkeypatch):
    monkeypatch.setattr(main, "get_media_info", lambda url: fake_info(url))
    r = client.post("/api/info", json={"url": "https://youtu.be/aqz-KE-bpKQ"})
    assert r.status_code == 200
    opt = r.json()["options"][0]
    assert opt["id"] == "720" and "format" not in opt and "stream_sizes" not in opt


def test_download_validates_option_id(client):
    r = client.post("/api/download", json={"url": URL, "option_id": "../../x"})
    assert r.status_code == 422


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "info_limiter", main.RateLimiter(2))
    for _ in range(2):
        client.post("/api/info", json={"url": "https://vimeo.com/1"})
    r = client.post("/api/info", json={"url": "https://vimeo.com/1"})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


@pytest.mark.parametrize("u", [
    "https://example.com/a.jpg",
    "https://i.ytimg.com.evil.com/a.jpg",
    "http://i.ytimg.com/a.jpg",
    "https://user@i.ytimg.com/a.jpg",
    "https://127.0.0.1/a.jpg",
])
def test_thumb_proxy_allow_list(client, u):
    assert client.get("/api/thumb", params={"u": u}).status_code == 400


def test_unknown_job(client):
    assert client.get("/api/file/" + "0" * 32).status_code == 404
    assert client.get("/api/progress/not-a-job").status_code == 404
    assert client.post("/api/jobs/" + "0" * 32 + "/cancel").status_code == 404


def test_full_download_flow(client, monkeypatch):
    monkeypatch.setattr(main, "get_media_info", lambda url: fake_info(url))

    def fake_download(url, option, out_dir: Path, on_progress, cancel=None, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        on_progress(ProgressEvent(stage="downloading", percent=50.0))
        on_progress(ProgressEvent(stage="merging", percent=100.0))
        path = out_dir / "Test video1 [aqz-KE-bpKQ].mp4"
        path.write_bytes(b"x" * 1234)
        return path

    monkeypatch.setattr(jobs, "download", fake_download)

    job_id = client.post("/api/download", json={"url": URL, "option_id": "720"}).json()["job_id"]
    for _ in range(50):
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["stage"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert state["stage"] == "done", state
    assert state["filename"] == "Test video1.mp4"

    # SSE stream ends with the terminal event
    sse = client.get(f"/api/progress/{job_id}").text
    assert sse.startswith("data: ") and '"stage":"done"' in sse

    r = client.get(f"/api/file/{job_id}")
    assert r.status_code == 200 and len(r.content) == 1234
    assert "Test%20video1.mp4" in r.headers["content-disposition"]

    hist = client.get("/api/history").json()
    assert len(hist) == 1 and hist[0]["option_id"] == "720" and hist[0]["filesize"] == 1234
    assert client.delete(f"/api/history/{hist[0]['id']}").status_code == 200
    assert client.get("/api/history").json() == []
    assert client.delete(f"/api/history/{hist[0]['id']}").status_code == 404


def test_failed_download_reports_friendly_error(client, monkeypatch):
    monkeypatch.setattr(main, "get_media_info", lambda url: fake_info(url))

    def boom(*a, **k):
        raise Exception("ERROR: [youtube] abc: Private video. Sign in if you've been granted access")

    monkeypatch.setattr(jobs, "download", boom)
    job_id = client.post("/api/download", json={"url": URL, "option_id": "720"}).json()["job_id"]
    for _ in range(50):
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["stage"] == "error":
            break
        time.sleep(0.05)
    assert state["error_code"] == "private"


@pytest.mark.parametrize("name, expected", [
    ("Big Buck Bunny [aqz-KE-bpKQ].mp4", "Big Buck Bunny.mp4"),
    ("Song [Official Video] [aqz-KE-bpKQ].mp3", "Song [Official Video].mp3"),
    ('a"b*c?<d> [abcdefghijk].m4a', "abcd.m4a"),
    (" [abcdefghijk].mp4", "download.mp4"),
])
def test_download_filename(name, expected):
    assert download_filename(Path(name)) == expected
