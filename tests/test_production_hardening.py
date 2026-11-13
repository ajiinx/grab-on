"""Comprehensive production hardening tests:
- Security: SSRF, IP validation, safe redirects, proxy headers
- Media reliability: Spotify international locales, PNG cover art tagging, embed resilience
- Backend stability: RateLimiter pruning, SQLite WAL/rollback, Job timeout & prune
- Global error handling & CORS
"""

import base64
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import config, jobs, main, spotify
from app.errors import UserFacingError
from app.extractor import validate_url
from app.models import DownloadOption, MediaInfo, ProgressEvent, SpotifyTrack
from app.storage import History


# --------------------------------------------------------------------------- #
# 1. Spotify International URLs & Resilience
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url, expected_kind, expected_id", [
    ("https://open.spotify.com/intl-es-419/track/4PTG3Z6ehGkBFwjybzWkR8", "track", "4PTG3Z6ehGkBFwjybzWkR8"),
    ("https://open.spotify.com/intl-fil/track/4PTG3Z6ehGkBFwjybzWkR8", "track", "4PTG3Z6ehGkBFwjybzWkR8"),
    ("https://open.spotify.com/intl-zh-TW/album/6XhjNHCyCDyyGJRM5mg40G", "album", "6XhjNHCyCDyyGJRM5mg40G"),
    ("https://open.spotify.com/intl-en-US/playlist/37i9dQZF1DXcBWIGoYBM5M", "playlist", "37i9dQZF1DXcBWIGoYBM5M"),
])
def test_spotify_bcp47_locale_urls(url, expected_kind, expected_id):
    platform, canonical = validate_url(url)
    assert platform == "spotify"
    assert canonical == f"https://open.spotify.com/{expected_kind}/{expected_id}"


def test_spotify_embed_initial_state_fallback(monkeypatch):
    raw_payload = json.dumps({
        "entity": {
            "title": "Fallback Song",
            "artists": [{"name": "Fallback Artist"}],
            "duration": 180000,
            "visualIdentity": {"image": [{"url": "https://i.scdn.co/image/fallback", "maxWidth": 640}]},
        }
    })
    b64_payload = base64.b64encode(raw_payload.encode()).decode()
    html_b64 = f'<html><body><script id="initial-state" type="text/plain">{b64_payload}</script></body></html>'

    monkeypatch.setattr(spotify, "_http_get", lambda url: html_b64.encode())
    track = spotify.get_track("4PTG3Z6ehGkBFwjybzWkR8")
    assert track.title == "Fallback Song"
    assert track.artists == ["Fallback Artist"]
    assert track.cover == "https://i.scdn.co/image/fallback"


def test_spotify_png_cover_tagging(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    fmt_info = spotify._cover_format(png_bytes)
    assert fmt_info is not None
    assert fmt_info[0] == "image/png"

    jpeg_bytes = b"\xff\xd8\xff\xe0"
    fmt_info_jpg = spotify._cover_format(jpeg_bytes)
    assert fmt_info_jpg is not None
    assert fmt_info_jpg[0] == "image/jpeg"


# --------------------------------------------------------------------------- #
# 2. Security: SSRF, IP Validation & Redirects
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("host, is_safe", [
    ("127.0.0.1", False),
    ("localhost", False),
    ("169.254.169.254", False),  # Cloud metadata
    ("10.0.0.1", False),
    ("192.168.1.1", False),
    ("172.16.0.1", False),
    ("0.0.0.0", False),
    ("::1", False),
    ("8.8.8.8", True),  # Public IP literal
    ("1.1.1.1", True),  # Public IP literal
])
def test_is_safe_ip_detection(host, is_safe):
    assert main._is_safe_ip(host) == is_safe
    assert spotify._is_safe_ip(host) == is_safe


def test_is_safe_ip_with_resolved_dns():
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
        assert main._is_safe_ip("example.com") is True
        assert spotify._is_safe_ip("example.com") is True


def test_thumb_redirect_handler_blocks_private_redirect():
    handler = main._ThumbRedirectHandler()
    req = MagicMock()
    req.headers = {}
    # Redirect to internal IP must return None (drop redirect)
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://127.0.0.1/evil.jpg") is None
    assert handler.redirect_request(req, None, 302, "Found", {}, "http://i.ytimg.com/evil.jpg") is None
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://evil.com/thumb.jpg") is None


def test_thumb_proxy_rejects_private_ip(monkeypatch):
    client = TestClient(main.app)
    # Mock _is_safe_ip to simulate DNS resolution to private IP
    monkeypatch.setattr(main, "_is_safe_ip", lambda host: False)
    r = client.get("/api/thumb", params={"u": "https://i.ytimg.com/vi/test/hqdefault.jpg"})
    assert r.status_code == 502


# --------------------------------------------------------------------------- #
# 3. RateLimiter Memory Management & Proxy IP Extraction
# --------------------------------------------------------------------------- #


def test_rate_limiter_pruning():
    limiter = main.RateLimiter(limit=5, window=0.1, max_keys=10)
    for i in range(5):
        assert limiter.allow("client_1")
    assert len(limiter._hits) == 1
    # Wait for window expiry
    time.sleep(0.15)
    limiter.allow("client_2")
    # client_1 should have been pruned
    assert "client_1" not in limiter._hits
    assert "client_2" in limiter._hits


def test_rate_limiter_max_keys_cap():
    limiter = main.RateLimiter(limit=5, window=10.0, max_keys=3)
    for i in range(3):
        assert limiter.allow(f"client_{i}")
    # 4th key exceeds max_keys without window expiry, rejected
    assert not limiter.allow("client_overflow")


def test_client_ip_with_trusted_proxies(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", {"127.0.0.1", "10.0.0.1"})

    req = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = {"x-forwarded-for": "203.0.113.195, 10.0.0.1"}
    assert main.client_ip(req) == "203.0.113.195"

    req.headers = {"x-real-ip": "198.51.100.4"}
    assert main.client_ip(req) == "198.51.100.4"

    # Untrusted remote client cannot spoof X-Forwarded-For
    req.client.host = "203.0.113.50"
    req.headers = {"x-forwarded-for": "1.1.1.1"}
    assert main.client_ip(req) == "203.0.113.50"


# --------------------------------------------------------------------------- #
# 4. Database Concurrency, WAL & Rollback Safety
# --------------------------------------------------------------------------- #


def test_history_wal_mode_and_concurrency(tmp_path):
    db_file = tmp_path / "test_wal.db"
    h = History(db_file)
    with h._lock:
        mode = h._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"

    item = h.add(
        url="https://youtube.com/watch?v=aqz-KE-bpKQ",
        platform="youtube",
        title="WAL Test Video",
        uploader="Test",
        thumbnail="https://i.ytimg.com/test.jpg",
        option_id="1080",
        option_label="1080p",
        ext="mp4",
        filesize=5000,
    )
    assert item.id >= 1
    assert h.get(item.id).title == "WAL Test Video"
    h.close()


def test_history_rollback_on_error(tmp_path):
    db_file = tmp_path / "test_rollback.db"
    h = History(db_file)

    mock_conn = MagicMock()
    mock_conn.execute.side_effect = sqlite3.OperationalError("Simulated disk error")
    h._conn = mock_conn

    with pytest.raises(sqlite3.OperationalError):
        h.add(
            url="https://x.com", platform="twitter", title="T", uploader=None,
            thumbnail=None, option_id="720", option_label="720p", ext="mp4", filesize=10
        )
    assert mock_conn.rollback.called
    assert not mock_conn.commit.called


# --------------------------------------------------------------------------- #
# 5. Job Execution Timeout & Orphan Pruning
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_job_execution_timeout(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(config, "JOB_TIMEOUT_SECONDS", 0.1)

    async def hung_lookup(url):
        await asyncio.sleep(2.0)
        return None

    h = History(tmp_path / "h.db")
    mgr = jobs.JobManager(tmp_path / "jobs", 2, h, hung_lookup, file_ttl_seconds=60)

    job = mgr.submit("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "720")
    for _ in range(50):
        if job.is_finished:
            break
        await asyncio.sleep(0.02)

    assert job.state.stage == "error"
    assert job.state.error_code == "timeout"
    await mgr.shutdown()
    h.close()


@pytest.mark.anyio
async def test_job_manager_prunes_orphaned_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_SECONDS", 10.0)
    h = History(tmp_path / "h.db")
    mgr = jobs.JobManager(tmp_path / "jobs", 2, h, lambda u: None, file_ttl_seconds=10)

    job = jobs.Job(
        id="a" * 32,
        url="https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        option_id="720",
        dir=tmp_path / "jobs" / ("a" * 32),
        created_at=time.time() - 30.0,  # 30 seconds old, > 2x timeout (20s)
    )
    job.dir.mkdir(parents=True, exist_ok=True)
    mgr.jobs[job.id] = job

    assert job.id in mgr.jobs
    await mgr.prune(now=time.time())
    # Orphaned hung job should be purged
    assert job.id not in mgr.jobs
    assert not job.dir.exists()
    await mgr.shutdown()
    h.close()


# --------------------------------------------------------------------------- #
# 6. Global Unhandled Exception Handling
# --------------------------------------------------------------------------- #


def test_global_unhandled_exception_sanitization(monkeypatch):
    client = TestClient(main.app, raise_server_exceptions=False)

    def crash(*args, **kwargs):
        raise RuntimeError("Internal database password secret_123 failed")

    monkeypatch.setattr(main, "lookup_info", crash)
    r = client.post("/api/info", json={"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"})
    assert r.status_code == 500
    data = r.json()
    assert data["error"] == "internal_error"
    # Verify sensitive traceback message is not leaked to client
    assert "secret_123" not in data["message"]
    assert "unexpected server error" in data["message"].lower()


# --------------------------------------------------------------------------- #
# 7. CLI Execution Test
# --------------------------------------------------------------------------- #


def test_cli_help(capsys):
    from app.cli import main as cli_main

    with pytest.raises(SystemExit) as exc:
        cli_main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Download from YouTube, Instagram, Spotify or Twitter/X" in captured.out
