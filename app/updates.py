"""yt-dlp update check and optional self-update.

YouTube and Instagram change often and break extractors, so an outdated yt-dlp
is the most common cause of failures. We check PyPI at startup and surface the
result in /api/health and the UI.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import urllib.request

log = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"


def _parse(version: str) -> tuple[int, ...]:
    # yt-dlp uses date versions: 2026.08.19 or 2026.08.19.1
    return tuple(int(p) for p in version.split(".") if p.isdigit())


def installed_version() -> str:
    from yt_dlp.version import __version__

    return __version__


def latest_version(timeout: float = 5.0) -> str | None:
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=timeout) as resp:  # fixed https URL
            return json.load(resp)["info"]["version"]
    except Exception as exc:  # noqa: BLE001 — offline is fine, just skip the check
        log.info("Could not check for yt-dlp updates: %s", exc)
        return None


def is_newer(latest: str | None, current: str) -> bool:
    if not latest:
        return False
    try:
        return _parse(latest) > _parse(current)
    except ValueError:
        return False


def self_update() -> bool:
    """Run `pip install -U yt-dlp` in this interpreter. Call before yt_dlp is imported."""
    log.info("Updating yt-dlp…")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "yt-dlp"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        log.warning("yt-dlp update failed: %s", result.stderr.strip()[-500:])
        return False
    return True
