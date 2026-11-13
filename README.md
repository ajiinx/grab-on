# Grab — Self-Hosted Media Downloader (Production Release)

A fast, lightweight, ad-free media downloading service built with **FastAPI**, **yt-dlp**, and **ffmpeg**. Designed for personal and home-server hosting.

Paste a **YouTube**, **Instagram**, **Spotify**, or **Twitter/X** link, choose from a clean list of curated format options (Best/4K, 1080p, 720p, 480p, MP3, M4A), and stream the file directly to your browser.

> **Legal Disclaimer:** For personal use only, with content you own or have the explicit right to download. Downloading may violate third-party terms of service. Not intended for public commercial hosting.

---

## Features

- **Multi-Platform Support**:
  - **YouTube**: Videos, Shorts, live/embed links, and playlists (up to 200 videos). Subtitle embedding & SRT downloads.
  - **Instagram**: Reels, single posts, and multi-video carousels.
  - **Spotify**: Tracks, albums, and playlists. Extracts public metadata & cover art, matches official audio on YouTube, and embeds ID3/MP4 metadata with artwork.
  - **Twitter / X**: Single videos, vertical videos, and multi-video status posts.
- **Production Hardened**:
  - **Zero-SSRF Architecture**: Strict CDN hostname allow-list with resolved IP validation blocking private/loopback/cloud metadata networks (`127.0.0.1`, `10.0.0.0/8`, `169.254.169.254`). Safe redirect validation prevents open redirect exploits.
  - **Concurrency & Deadlock Prevention**: Configurable concurrency semaphore with per-job execution timeouts preventing hung tasks from locking workers.
  - **High-Performance SQLite Store**: History database with WAL (`Write-Ahead Logging`) mode, busy timeouts, and automatic schema migrations.
  - **Sliding-Window Rate Limiting**: Per-IP throttling with automatic memory pruning to prevent memory leaks, plus reverse proxy header support (`X-Forwarded-For`).
  - **Streaming ZIP Bundling**: Multi-file downloads are zipped dynamically on the fly without temporary disk duplication.
  - **Security Headers & Auth**: Built-in CSP, strict frame options, and timing-safe HTTP Basic Auth.

---

## Quick Start (Development & Local Run)

### Prerequisites

- **Python 3.11+** (tested on Python 3.13)
- **ffmpeg & ffprobe**
- **JavaScript Runtime (Deno or Node.js)** — recommended for YouTube player challenge solving

### 1. Install System Dependencies

```bash
# Windows
winget install Gyan.FFmpeg denoland.deno

# macOS
brew install ffmpeg deno

# Debian / Ubuntu Linux
sudo apt update && sudo apt install -y ffmpeg
# Deno install:
curl -fsSL https://deno.land/install.sh | sh
```

### 2. Setup Virtual Environment

```bash
# Clone the repository and navigate into it
cd grab-on

# Create virtual environment (Python 3.11+)
python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate       # Linux / macOS
# On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
```

### 4. Run Development Server

```bash
python run.py
# Or:
python -m app
```

Open **http://127.0.0.1:8000** in your browser.

---

## Production Deployment

### Option 1: Docker & Docker Compose (Recommended)

The included `Dockerfile` builds a production image based on `python:3.12-slim` containing `ffmpeg`, `ca-certificates`, and `Deno`, running under an unprivileged `app` user (UID 1000).

```bash
# 1. Create persistent host directories with proper ownership
mkdir -p data downloads

# 2. Start container in detached mode
docker compose up -d --build
```

Container features:
- Runs as non-root user `app`.
- Internal container healthcheck with automated authentication support (`/api/health`).
- Auto-updates `yt-dlp` on startup (`YTDLP_AUTO_UPDATE=true`).
- Persistent storage for `data/history.db` and temporary job files in `downloads/`.

To inspect logs:
```bash
docker compose logs -f downloader
```

To stop:
```bash
docker compose down
```

### Option 2: Systemd Service (Linux Server)

Create `/etc/systemd/system/grab.service`:

```ini
[Unit]
Description=Grab Media Downloader
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/grab-on
EnvironmentFile=/opt/grab-on/.env
ExecStart=/opt/grab-on/.venv/bin/python -m app
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grab
```

### Option 3: Remote Access with Tailscale (Recommended over Public Exposure)

Do not expose the application directly to the public internet without authentication. Tailscale provides end-to-end encrypted mesh networking:

```bash
# Keep HOST=127.0.0.1 and serve via Tailscale HTTPS
tailscale serve --bg 8000
```

Access the service from your phone or laptop at `https://your-node-name.ts.net`.

---

## Environment Variables Configuration

Configure these in your `.env` file or container environment:

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Network interface to bind to (`0.0.0.0` inside Docker). |
| `PORT` | `8000` | HTTP port to listen on. |
| `APP_PASSWORD` | *(empty)* | If set, all endpoints require HTTP Basic Auth with this password (any username). |
| `ALLOWED_ORIGINS` | *(empty)* | Comma-separated list of allowed CORS origins (e.g. `https://myfrontend.com`). Defaults to same-origin. |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | Comma-separated list of trusted proxy IPs/CIDRs authorized to pass `X-Forwarded-For` and `X-Real-IP`. |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Maximum concurrent background download jobs permitted simultaneously. |
| `JOB_TIMEOUT_SECONDS` | `1800` | Maximum execution time in seconds for a download job before it is terminated (prevents hung tasks). |
| `FILE_TTL_MINUTES` | `30` | Time in minutes finished downloads remain on disk for browser retrieval before automated deletion. |
| `RATE_LIMIT_INFO` | `30` | Requests per minute per IP for `/api/info`. |
| `RATE_LIMIT_DOWNLOAD` | `10` | Requests per minute per IP for `/api/download`. |
| `EMBED_METADATA` | `true` | Embed title, artist, date, chapters, and thumbnail artwork into downloaded media. |
| `YTDLP_AUTO_UPDATE` | `false` | Run `pip install -U yt-dlp` automatically upon application startup. |
| `COOKIES_FILE` | *(empty)* | Path to Netscape-format `cookies.txt` for login-gated Instagram or age-restricted YouTube media. |
| `COOKIES_FROM_BROWSER` | *(empty)* | Browser name (e.g. `firefox`, `chrome`) to extract cookies from directly (bare-metal only). |
| `FFMPEG_LOCATION` | *(empty)* | Custom directory containing `ffmpeg` and `ffprobe` binaries if not in system `PATH`. |
| `JS_RUNTIMES` | `deno,node,bun` | Priority order of JavaScript runtimes for YouTube challenge solving. |
| `DATA_DIR` | `./data` | Directory where SQLite history database (`history.db`) is stored. |
| `DOWNLOAD_DIR` | `./downloads` | Root scratch directory for temporary per-job download folders. |

---

## Database Migrations

The application manages SQLite migrations automatically via `app/storage.py`:

- The history table schema:
  ```sql
  CREATE TABLE IF NOT EXISTS history (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      url          TEXT NOT NULL,
      platform     TEXT NOT NULL,
      title        TEXT NOT NULL,
      uploader     TEXT,
      thumbnail    TEXT,
      option_id    TEXT NOT NULL,
      option_label TEXT NOT NULL,
      ext          TEXT NOT NULL,
      filesize     INTEGER,
      item_count   INTEGER,
      created_at   TEXT NOT NULL
  );
  ```
- Migrations run automatically on application startup inside a thread-safe transaction.
- SQLite is configured with **Write-Ahead Logging (WAL)** and **busy timeouts** for high concurrency without lock contention.
- No manual SQL scripts or external migration tools (e.g. Alembic) are required.

To backup or reset history:
```bash
# Backup
cp data/history.db data/history.db.bak

# Reset history
rm data/history.db
```

---

## Command Line Interface (CLI)

The CLI uses the exact same core engine without needing a web server:

```bash
# Inspect available curated options for any URL
python -m app.cli "https://youtu.be/aqz-KE-bpKQ"

# Download a specific quality tier into ./downloads
python -m app.cli "https://youtu.be/aqz-KE-bpKQ" -o 1080

# Download MP3 audio directly to a custom folder
python -m app.cli "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8" -o mp3 --out /media/music
```

---

## Testing & Quality Assurance

Run the comprehensive test suite with `pytest`:

```bash
# Run all tests
pytest

# Run tests with detailed verbosity
pytest -v

# Run production hardening tests specifically
pytest tests/test_production_hardening.py -v
```

All 220 automated tests cover:
- URL validation and BCP-47 locale handling (`intl-es-419`, `intl-pt-BR`, `intl-fil`).
- Format curation, short-side aspect ratio handling, and size estimation.
- Playlists, Instagram carousels, and subtitle conversions.
- Spotify metadata parsing, YouTube matching, and PNG/JPEG cover art tagging.
- Twitter/X video and multi-video post curation.
- SSRF prevention, IP resolution, and safe redirect validation.
- Database WAL mode, transaction rollback, and rate limiter memory leak prevention.
- Job timeouts and orphaned scratch file cleanup.

---

## Troubleshooting & Maintenance

| Issue | Cause | Solution |
|---|---|---|
| **"ffmpeg isn't installed" warning** | `ffmpeg` binary not found in system `PATH`. | Install ffmpeg via OS package manager or set `FFMPEG_LOCATION=/path/to/bin`. |
| **"Confirm you're not a bot" / "Sign in"** | YouTube or Instagram bot detection triggered. | Export Netscape cookies from your browser and set `COOKIES_FILE=cookies.txt`. |
| **Downloads suddenly fail across a platform** | Upstream site changed layout or API. | Update yt-dlp: `pip install -U "yt-dlp[default]"` and restart server. |
| **Instagram video missing audio** | Instagram often strips audio for unauthenticated requests. | Provide cookies via `COOKIES_FILE=cookies.txt`. |
| **Only low resolutions available on YouTube** | Missing JavaScript runtime for player cipher challenge. | Install Deno (`deno`) or Node.js (`node`). |
| **Download doesn't trigger automatically on iOS** | Mobile Safari security policy on programmatic downloads. | Tap the **Save file** button displayed upon completion. |
| **Spotify link rejected** | Short URL (`spotify.link`) or unsupported link type. | Open the short link in a browser and copy the full `open.spotify.com/track/...` link. |
| **Address already in use** | Another service is using port 8000. | Change `PORT=8001` in `.env`. |

---

## Deployment Rollback Instructions

If a rollback is required during a deployment:

1. **Docker Deployment**:
   ```bash
   # Revert to previous image or commit
   git checkout <previous-release-tag>
   docker compose up -d --build
   ```
2. **Systemd Deployment**:
   ```bash
   sudo systemctl stop grab
   git checkout <previous-release-tag>
   pip install -r requirements.txt
   sudo systemctl start grab
   ```
3. **Database Compatibility**:
   The SQLite schema is strictly backward-compatible. Rolling back application code will not corrupt `data/history.db`.
