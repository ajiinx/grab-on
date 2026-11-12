"""Command-line MVP: list curated options for a link and download one.

Usage:
    python -m app.cli URL                 # list options (and prompt if interactive)
    python -m app.cli URL -o 720          # download the 720p option
    python -m app.cli URL -o mp3 --out ./downloads
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DOWNLOAD_DIR, ffmpeg_path
from .downloader import DownloadCancelled, download
from .errors import UserFacingError
from .extractor import get_media_info
from .models import ProgressEvent


def human_size(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return "?"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def print_progress(ev: ProgressEvent) -> None:
    if ev.stage == "downloading" and ev.percent is not None:
        speed = f"{human_size(ev.speed)}/s" if ev.speed else ""
        eta = f"ETA {human_duration(ev.eta)}" if ev.eta else ""
        bar = "#" * int(ev.percent / 4)
        sys.stdout.write(f"\r  [{bar:<25}] {ev.percent:5.1f}%  {speed:>12}  {eta:<12}")
        sys.stdout.flush()
    elif ev.stage in ("merging", "converting", "processing"):
        sys.stdout.write(f"\r  {ev.message or ev.stage:<70}")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download from YouTube, Instagram, Spotify or Twitter/X.")
    parser.add_argument("url")
    parser.add_argument("-o", "--option", help="option id to download (e.g. best, 1080, 720, mp3, m4a)")
    parser.add_argument("--out", type=Path, default=DOWNLOAD_DIR, help="output directory")
    args = parser.parse_args(argv)

    if not ffmpeg_path():
        print("error: ffmpeg not found — install it or set FFMPEG_LOCATION", file=sys.stderr)
        return 2

    try:
        info = get_media_info(args.url)
    except UserFacingError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 1

    print(f"\n{info.title}")
    print(f"  {info.uploader or '?'} · {human_duration(info.duration)} · {info.platform}\n")
    for opt in info.options:
        print(f"  {opt.id:<6} {opt.label:<22} {opt.detail:<28} ~{human_size(opt.filesize)}")
    print()

    choice = args.option
    if choice is None:
        if not sys.stdin.isatty():
            return 0
        try:
            choice = input("Option id to download (blank to quit): ").strip()
        except EOFError:
            choice = ""
        if not choice:
            return 0

    option = next((o for o in info.options if o.id == choice), None)
    if option is None:
        print(f"error: no option '{choice}' for this video", file=sys.stderr)
        return 1

    try:
        path = download(info.url, option, args.out, print_progress)
    except DownloadCancelled:
        print("\ncancelled")
        return 130
    except UserFacingError as e:
        print(f"\nerror: {e.message}", file=sys.stderr)
        return 1
    print(f"\n  saved: {path} ({human_size(path.stat().st_size)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
