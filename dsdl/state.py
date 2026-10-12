"""SQLite state kept on the output drive: items, per-file manifests, and run history."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
    key TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT,
    local_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | listed | complete | failed
    version TEXT,
    total_bytes INTEGER,                       -- NULL = unknown
    file_count INTEGER,
    error TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    listed_at REAL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS files(
    item_key TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER,
    url TEXT NOT NULL,
    algo TEXT,
    checksum TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(item_key, path)
);
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY,
    task TEXT,
    started REAL,
    seconds REAL,
    bytes INTEGER
);
"""

PENDING, LISTED, COMPLETE = "pending", "listed", "complete"
FAILED = "failed"            # transient problem (network, partial download) — retried every run
UNAVAILABLE = "unavailable"  # permanent (not found, gated, private) — retried with --retry-failed or new tokens


@dataclass
class FileTotals:
    files: int
    total_bytes: int  # sum of known sizes
    unknown: int      # files with unknown size
    done_files: int
    done_bytes: int


@dataclass
class ItemState:
    key: str
    task: str
    source: str
    status: str
    version: str | None
    total_bytes: int | None
    file_count: int | None
    error: str | None
    meta: dict
    local_path: str | None
    completed_at: float | None


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._last_commit = time.monotonic()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # -- plumbing -----------------------------------------------------------------
    def commit(self) -> None:
        with self._lock:
            self._db.commit()
            self._last_commit = time.monotonic()

    def _maybe_commit(self) -> None:
        if time.monotonic() - self._last_commit > 2.0:
            self.commit()

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()

    # -- items --------------------------------------------------------------------
    def upsert_item(self, key: str, task: str, source: str, url: str, local_path: str) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO items(key, task, source, url, local_path) VALUES(?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET task=excluded.task, source=excluded.source,
                   url=excluded.url, local_path=excluded.local_path""",
                (key, task, source, url, local_path),
            )

    def item(self, key: str) -> ItemState | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        return _to_item(r) if r else None

    def items(self, task: str | None = None) -> list[ItemState]:
        with self._lock:
            if task:
                rows = self._db.execute("SELECT * FROM items WHERE task=? ORDER BY key", (task,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM items ORDER BY task, key").fetchall()
        return [_to_item(r) for r in rows]

    def set_status(self, key: str, status: str, error: str | None = None, **fields) -> None:
        sets, vals = ["status=?", "error=?"], [status, error]
        if status == COMPLETE:
            sets.append("completed_at=?")
            vals.append(time.time())
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        with self._lock:
            self._db.execute(f"UPDATE items SET {', '.join(sets)} WHERE key=?", (*vals, key))
            self.commit()

    def update_meta(self, key: str, **meta) -> dict:
        with self._lock:
            cur = self.item(key)
            merged = {**(cur.meta if cur else {}), **meta}
            self._db.execute("UPDATE items SET meta=? WHERE key=?", (json.dumps(merged), key))
            self.commit()
        return merged

    # -- manifests ----------------------------------------------------------------
    def set_manifest(self, key: str, version: str | None, files: list) -> None:
        """Replace an item's file list, keeping `done` for files whose identity didn't change."""
        with self._lock:
            old = {
                r["path"]: r
                for r in self._db.execute("SELECT * FROM files WHERE item_key=?", (key,)).fetchall()
            }
            self._db.execute("DELETE FROM files WHERE item_key=?", (key,))
            rows = []
            for f in unique_paths(files):
                o = old.get(f.path)
                same = (
                    o is not None and o["done"] and o["size"] == f.size
                    and (o["checksum"] or None) == (f.checksum or None)
                    and (f.checksum or o["url"] == f.url)
                )
                rows.append((key, f.path, f.size, f.url, f.algo, f.checksum, 1 if same else 0))
            self._db.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?)", rows)
            sizes = [f.size for f in files]
            total = None if any(s is None for s in sizes) else sum(sizes)
            self._db.execute(
                "UPDATE items SET version=?, total_bytes=?, file_count=?, listed_at=?, status='listed', error=NULL WHERE key=?",
                (version, total, len(files), time.time(), key),
            )
            self.commit()

    def has_manifest(self, key: str) -> bool:
        with self._lock:
            r = self._db.execute("SELECT listed_at FROM items WHERE key=?", (key,)).fetchone()
        return bool(r and r["listed_at"])

    def files(self, key: str, pending_only: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM files WHERE item_key=?" + (" AND done=0" if pending_only else "") + " ORDER BY path"
        with self._lock:
            return self._db.execute(q, (key,)).fetchall()

    def reset_files(self, key: str) -> None:
        with self._lock:
            self._db.execute("UPDATE files SET done=0 WHERE item_key=?", (key,))
            self.commit()

    def mark_file(self, key: str, path: str, done: bool = True) -> None:
        with self._lock:
            self._db.execute("UPDATE files SET done=? WHERE item_key=? AND path=?", (1 if done else 0, key, path))
            self._maybe_commit()

    def file_totals(self, key: str) -> FileTotals:
        with self._lock:
            r = self._db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS total, "
                "COALESCE(SUM(CASE WHEN size IS NULL THEN 1 END),0) AS unknown, "
                "COALESCE(SUM(done),0) AS done_n, COALESCE(SUM(CASE WHEN done=1 THEN size END),0) AS done_b "
                "FROM files WHERE item_key=?",
                (key,),
            ).fetchone()
        return FileTotals(int(r["n"]), int(r["total"]), int(r["unknown"]), int(r["done_n"]), int(r["done_b"]))

    # -- run history (for ETA estimates) ------------------------------------------
    def record_run(self, task: str, started: float, seconds: float, nbytes: int) -> None:
        with self._lock:
            self._db.execute("INSERT INTO runs(task, started, seconds, bytes) VALUES(?,?,?,?)", (task, started, seconds, nbytes))
            self.commit()

    def avg_speed(self) -> float | None:
        """Bytes/second over recent runs that moved a meaningful amount of data."""
        with self._lock:
            rows = self._db.execute(
                "SELECT seconds, bytes FROM runs WHERE bytes > 100000000 AND seconds > 30 ORDER BY id DESC LIMIT 10"
            ).fetchall()
        secs = sum(r["seconds"] for r in rows)
        return (sum(r["bytes"] for r in rows) / secs) if secs else None


def unique_paths(files: list) -> list:
    """Rename later duplicates (`a.txt` → `a~2.txt`) so two remote files never share a local path."""
    seen: set[str] = set()
    out = []
    for f in files:
        path = f.path
        if path.lower() in seen:  # case-insensitive: macOS/exFAT can't hold both
            stem, dot, ext = path.rpartition(".") if "." in path.rsplit("/", 1)[-1] else (path, "", "")
            n = 2
            while (cand := f"{stem}~{n}{dot}{ext}").lower() in seen:
                n += 1
            f = replace(f, path=cand)
        seen.add(f.path.lower())
        out.append(f)
    return out


def _to_item(r: sqlite3.Row) -> ItemState:
    return ItemState(
        key=r["key"], task=r["task"], source=r["source"], status=r["status"], version=r["version"],
        total_bytes=r["total_bytes"], file_count=r["file_count"], error=r["error"],
        meta=json.loads(r["meta"] or "{}"), local_path=r["local_path"], completed_at=r["completed_at"],
    )
