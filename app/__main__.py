"""Entry point: `python -m app`. Binds to 127.0.0.1:8000 unless HOST/PORT say otherwise."""

from __future__ import annotations

import ipaddress
import logging
import sys

from . import config, updates


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log = logging.getLogger("app")

    if config.YTDLP_AUTO_UPDATE:
        updates.self_update()  # before anything imports yt_dlp

    if not config.ffmpeg_path():
        print(
            "\n  ERROR: ffmpeg was not found.\n"
            "  Install it (Windows: winget install Gyan.FFmpeg | macOS: brew install ffmpeg |\n"
            "  Debian/Ubuntu: sudo apt install ffmpeg) or set FFMPEG_LOCATION.\n"
            "  The server will start, but downloads will fail until ffmpeg is available.\n",
            file=sys.stderr,
        )

    try:
        loopback = ipaddress.ip_address(config.HOST).is_loopback
    except ValueError:
        loopback = config.HOST == "localhost"
    if not loopback and not config.APP_PASSWORD:
        log.warning("Listening on %s without APP_PASSWORD — anyone who can reach this address can use it.", config.HOST)

    import uvicorn

    shown = "127.0.0.1" if config.HOST in ("0.0.0.0", "::") else config.HOST
    print(f"\n  Media Downloader running at http://{shown}:{config.PORT}\n")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
