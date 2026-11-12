# Grab — self-hosted YouTube, Instagram, Spotify & Twitter/X downloader

A small, ad-free web app for personal use. Paste a YouTube, Instagram, Spotify or
Twitter/X link, pick from a short list of options (Best/4K, 1080p, 720p, 480p, MP3, M4A) and
the file downloads to your browser. Runs on your own machine; built on
[yt-dlp](https://github.com/yt-dlp/yt-dlp) and ffmpeg.

> **Legal:** personal use only, for content you own or have the right to download.
> Downloading may violate YouTube's, Instagram's or Spotify's terms of service.
> Not intended to be hosted publicly.

---

## Quick start (local, Windows/macOS/Linux)

Requirements: **Python 3.11+**, **ffmpeg**, and ideally a JavaScript runtime
(**Deno** or **Node.js**) — YouTube needs one to unlock all formats.

```bash
# 1. ffmpeg (pick your OS)
winget install Gyan.FFmpeg          # Windows
brew install ffmpeg                 # macOS
sudo apt install ffmpeg             # Debian/Ubuntu

# 2. Python environment
python -m venv .venv
.venv\Scripts\activate              # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# 3. (optional) settings
copy .env.example .env              # macOS/Linux: cp .env.example .env

# 4. Run
python -m app
```

Open **http://127.0.0.1:8000**. The server only listens on this computer by default.

**Windows shortcut:** double-click `start.bat`. It sets up `.venv` on first run,
starts the server and opens the browser. Close the window to stop it. Right-click
→ *Send to → Desktop (create shortcut)* for a desktop icon.

### Command line

The same engine works without the web UI:

```bash
python -m app.cli "https://youtu.be/aqz-KE-bpKQ"           # list options
python -m app.cli "https://youtu.be/aqz-KE-bpKQ" -o 720     # download 720p into ./downloads
python -m app.cli "https://youtu.be/aqz-KE-bpKQ" -o mp3 --out D:\Music
```

## Docker

```bash
docker compose up -d --build
```

Then open http://127.0.0.1:8000. The image includes ffmpeg and Deno, runs as a
non-root user (uid 1000) and auto-updates yt-dlp on every start
(`YTDLP_AUTO_UPDATE=true`). History lives in `./data`, temporary files in
`./downloads`. On Linux, create those folders first so they are owned by you:
`mkdir -p data downloads`.

Settings go in `.env` next to `docker-compose.yml` (see `.env.example`).

## Using it

1. Paste a link — `youtube.com/watch`, `youtu.be`, `/shorts/`, a YouTube
   `/playlist?list=…`, an Instagram `/reel/` or `/p/` post, or a Twitter/X post. Pasting fetches
   automatically.
2. Pick an option. Only formats that actually exist for that video are shown, with
   size estimates. Video options prefer H.264 + AAC so files play everywhere;
   above 1080p YouTube only offers VP9/AV1, which is still saved as MP4 (MKV if needed).
3. Watch progress (download → merging/converting). The file is saved by your
   browser when done; tap **Save file** if it didn't start (some mobile browsers).
4. **History** lists past downloads: tap the title to see the options again, the
   arrow to download the same option again, or the bin to remove the entry.

### Playlists and carousels

- Paste a playlist link to get a checklist of its videos (up to 200; private and
  deleted ones are skipped). Pick some or all, then an option. Videos download one
  after another; if one fails, the rest still go through.
- Several files arrive as one **ZIP** (built while streaming, so no extra disk
  space), and each file also gets its own link — handier on a phone.
- Playlist options say "1080p — or lower if unavailable", because each video's
  formats are only known once it downloads.
- A `watch?v=…&list=…` link opens that one video, with an **Open the whole
  playlist** link.
- Instagram posts with several videos (carousels) work the same way; a carousel
  with one video is treated as a single video.

### Spotify

Paste an `open.spotify.com` **track, album or playlist** link.

Spotify's own audio is DRM-protected and is never downloaded. Instead the app:

1. reads the song details from Spotify's public embed page (title, artists,
   duration, cover art — no Spotify account or API key needed);
2. searches YouTube for the same recording and scores the results: duration must
   match within a few seconds, the artist's own channel / "Artist - Topic" uploads
   win over re-uploads, and live, cover, remix, karaoke, sped-up etc. versions are
   ruled out unless the Spotify title says so;
3. downloads that audio and writes **Spotify's** title, artist, album, release date,
   track number and cover art into the file, named `Artist - Title.mp3`.

For a single track the preview shows **"Audio from YouTube: …"** with a link, so a
wrong match is easy to spot before downloading. Albums and playlists get the same
checklist as YouTube playlists; each track is matched when it downloads, and a
track that can't be found is skipped and reported.

Options are **M4A** (the YouTube audio as-is — best quality, no re-encode) and
**MP3** (re-encoded for compatibility; it can't be better than the source,
typically ~130–160 kbps AAC/Opus). Limitations: Spotify's embed lists roughly the
first 100 tracks of a playlist; artist pages, podcasts and short `spotify.link`
links aren't supported (open the short link and copy the full URL).

### Subtitles

When a video has subtitles, a **Subtitles** picker appears. It lists uploaded
subtitles plus YouTube's auto-generated captions in the original language.

- Choose a language, then any video option: the subtitles are embedded in the MP4
  as a soft track you can switch on in your player.
- Or tap **Subtitles (SRT file)** to download just the subtitles.

### Tags and cover art

Downloads get the title, channel (artist), upload date, source URL and chapters
written into the file, and the thumbnail embedded as cover art — so MP3s show
artwork in music players. Disable with `EMBED_METADATA=false`.

Finished files are deleted from the server after `FILE_TTL_MINUTES` (default 30)
and on every restart — the server is a pass-through, not a library.

## Phone access with Tailscale

Don't expose the app to the public internet. Use [Tailscale](https://tailscale.com/)
so only your own devices can reach it.

1. Install Tailscale on the computer running the app and on your phone; sign in to
   both with the same account.
2. Keep the app on `127.0.0.1` and let Tailscale proxy it with HTTPS:

   ```bash
   tailscale serve --bg 8000
   ```

   Tailscale prints a URL like `https://your-pc.tailnet-name.ts.net` — open it on your
   phone. HTTPS also enables the **Paste** button (browsers only allow clipboard
   access on secure pages). Stop with `tailscale serve --https=443 off`.
3. Optional extra lock: set `APP_PASSWORD` in `.env` and restart. The browser will
   ask for it once (username can be anything).

*Alternative:* set `HOST=0.0.0.0` (or your Tailscale IP, `100.x.y.z`) and open
`http://100.x.y.z:8000`. Always set `APP_PASSWORD` if you do this.

Tip: on Android you can bookmark `https://…ts.net/?url=` — the app reads a `url`
query parameter and fetches it automatically.

## Keeping yt-dlp up to date

YouTube and Instagram change often, and an old yt-dlp is the #1 cause of failures.
The app checks PyPI on startup and shows a banner when an update exists.

```bash
pip install -U "yt-dlp[default]"            # local
docker compose restart                       # Docker (auto-updates on start)
```

Or set `YTDLP_AUTO_UPDATE=true` to upgrade on every start.

## Cookies (age-restricted / login-only content)

Some content needs a logged-in session: private or login-gated Instagram posts,
age-restricted YouTube videos, or YouTube's "confirm you're not a bot" check.
Instagram also frequently strips audio or refuses requests from logged-out clients.

**Option A — read from your browser** (local runs only):

```ini
COOKIES_FROM_BROWSER=firefox            # or chrome, edge, brave, "chrome:Profile 1"
```

Chrome/Edge on Windows lock their cookie database while running; Firefox is the
most reliable choice.

**Option B — cookies.txt file:**

1. Export cookies for `youtube.com` / `instagram.com` in Netscape format with a
   browser extension such as "Get cookies.txt LOCALLY".
2. Save as `cookies.txt` in this folder (it's git-ignored) and set
   `COOKIES_FILE=cookies.txt`. For Docker, uncomment the cookies volume in
   `docker-compose.yml` and set `COOKIES_FILE_IN_CONTAINER=/app/cookies.txt`.

Cookies are as sensitive as your password: **never commit them** (`.gitignore`
already excludes `cookies*.txt` and `.env`). Using an account for downloading can
get it rate-limited; consider a secondary account.

## Configuration

All settings are environment variables (or `.env`); see `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address |
| `APP_PASSWORD` | — | Require HTTP Basic auth (any username) |
| `COOKIES_FILE` / `COOKIES_FROM_BROWSER` | — | See *Cookies* |
| `FFMPEG_LOCATION` | — | Folder with ffmpeg if it isn't on PATH |
| `JS_RUNTIMES` | `deno,node,bun` | JS runtimes yt-dlp may use for YouTube |
| `YTDLP_AUTO_UPDATE` | `false` (Docker: `true`) | `pip install -U yt-dlp` on start |
| `EMBED_METADATA` | `true` | Write tags, chapters and cover art into files |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel downloads |
| `FILE_TTL_MINUTES` | `30` | How long finished files are kept |
| `RATE_LIMIT_INFO` / `RATE_LIMIT_DOWNLOAD` | `30` / `10` | Requests per minute per IP |

## API

| Method | Path | |
| --- | --- | --- |
| `POST` | `/api/info` | `{url}` → title, thumbnail, duration, uploader, curated `options`, `subtitles`; playlists add `kind: "playlist"` and `entries` |
| `POST` | `/api/download` | `{url, option_id, items?, subtitle?}` → `{job_id}` (`items`: playlist indices) |
| `GET` | `/api/progress/{job_id}` | Server-Sent Events: `{stage, percent, speed, eta, …}` |
| `GET` | `/api/jobs/{job_id}` | Current job state (polling alternative) |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel a queued/running job |
| `GET` | `/api/file/{job_id}` | The finished file (streamed, supports Range); a ZIP for multi-file playlist jobs |
| `GET` | `/api/file/{job_id}/{n}` | File *n* of a playlist job |
| `GET` / `DELETE` | `/api/history`, `/api/history/{id}` | Download history |
| `GET` | `/api/health` | yt-dlp/ffmpeg versions, update availability |

## Why not Vercel / serverless?

Downloads take minutes (serverless functions time out), need ffmpeg and scratch
disk, and YouTube/Instagram aggressively block datacenter IPs. Your home connection
is the right place to run this.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "ffmpeg isn't installed" banner / red dot | Install ffmpeg (above) or set `FFMPEG_LOCATION`, restart |
| "Confirm you're not a bot" / "requires logging in" | Configure cookies |
| Everything suddenly fails for one site | Update yt-dlp |
| Instagram video has no audio option | Instagram hid the audio track from a logged-out request — configure cookies |
| Only low resolutions on YouTube | Install Deno or Node.js so yt-dlp can solve YouTube's JS challenge |
| Download doesn't start on iPhone | Tap **Save file**; Safari saves into the Files app → Downloads |
| "Playlist is empty" | It's private, or all its videos are private/deleted |
| No Subtitles picker | The video has no uploaded subtitles or original-language captions |
| Spotify: wrong song downloaded | Check the "Audio from YouTube" link; rare for official releases, likelier for obscure tracks |
| Spotify: "Couldn't read that … from Spotify" | Private playlist, or Spotify changed its embed page (update the app) |
| `Address already in use` | Another app uses port 8000 — set `PORT=8001` |

Run the tests with `pytest`.

## Project layout

```
app/
  main.py         FastAPI routes, auth, rate limiting, SSE, thumbnail proxy
  extractor.py    URL validation + yt-dlp info + option curation
  spotify.py      Spotify metadata, YouTube matching, tagging
  downloader.py   one download: yt-dlp options, progress hooks, ffmpeg steps
  jobs.py         background queue (concurrency limit, cancel, cleanup)
  storage.py      SQLite history + temp folder cleanup
  models.py       pydantic models
  config.py       environment settings
  cli.py          command-line interface
  static/         index.html, style.css, app.js
tests/            URL validation, option curation, API, playlists/subtitles, Spotify
```
