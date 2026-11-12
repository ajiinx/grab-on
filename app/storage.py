"""Download history (SQLite) and temp-file cleanup."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import HistoryItem

log = logging.getLogger(__name__)

_SCHEMA = """
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
)
"""
# Columns added after the first release, for databases created earlier.
_MIGRATIONS = {"item_count": "ALTER TABLE history ADD COLUMN item_count INTEGER"}


class History:
    """Tiny thread-safe history store. One connection, guarded by a lock."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(history)")}
            for column, ddl in _MIGRATIONS.items():
                if column not in columns:
                    self._conn.execute(ddl)
            self._conn.commit()

    def add(
        self,
        *,
        url: str,
        platform: str,
        title: str,
        uploader: str | None,
        thumbnail: str | None,
        option_id: str,
        option_label: str,
        ext: str,
        filesize: int | None,
        item_count: int | None = None,
    ) -> HistoryItem:
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history (url, platform, title, uploader, thumbnail, option_id, option_label, ext,"
                " filesize, item_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, platform, title, uploader, thumbnail, option_id, option_label, ext, filesize, item_count, created),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        return self.get(row_id)  # type: ignore[return-value]

    def get(self, item_id: int) -> HistoryItem | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM history WHERE id = ?", (item_id,)).fetchone()
        return HistoryItem(**dict(row)) if row else None

    def list(self, limit: int = 100) -> list[HistoryItem]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [HistoryItem(**dict(r)) for r in rows]

    def delete(self, item_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM history WHERE id = ?", (item_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _ZipSink:
    """Write-only buffer that ZipFile writes into; we drain it as we go.

    It has no tell()/seek(), so zipfile switches to streaming mode (sizes go
    in data descriptors after each file) and never needs the whole archive in
    memory or on disk.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def zip_stream(files: list[tuple[str, Path]], chunk_size: int = 1 << 20) -> Iterator[bytes]:
    """Yield a ZIP archive of `files` ([(name_in_zip, path)]) chunk by chunk.

    Media is already compressed, so entries are stored, not deflated — this is
    fast and the archive size is predictable.
    """
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:  # type: ignore[arg-type]
        for name, path in files:
            info = zipfile.ZipInfo.from_file(path, arcname=name)
            info.compress_type = zipfile.ZIP_STORED
            with path.open("rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                while chunk := src.read(chunk_size):
                    dst.write(chunk)
                    yield sink.drain()
            yield sink.drain()
    yield sink.drain()  # central directory


def remove_dir(path: Path) -> None:
    """Delete a job directory, ignoring errors (e.g. a file still open on Windows)."""
    shutil.rmtree(path, ignore_errors=True)


def clean_jobs_dir(jobs_dir: Path) -> int:
    """Remove leftovers from previous runs. Returns how many entries were removed."""
    jobs_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for child in jobs_dir.iterdir():
        if child.is_dir():
            remove_dir(child)
        else:
            child.unlink(missing_ok=True)
        count += 1
    if count:
        log.info("Removed %d leftover job folder(s) from %s", count, jobs_dir)
    return count
