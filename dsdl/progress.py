"""Live progress display: items bar, data bar (bytes/speed/ETA), one bar per active item and big file."""

from __future__ import annotations

import threading

from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.rule import Rule
from rich.table import Column

BIG_FILE = 256 * 1000**2  # files at least this large get their own bar


def fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} PB"


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class SyncUI:
    def __init__(self, console: Console, label: str, n_items: int, total_bytes: int | None, have_bytes: int) -> None:
        self.console = console
        name_col = Column(width=10, no_wrap=True)
        self.summary_items = Progress(
            TextColumn("[bold]{task.description}", table_column=name_col),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("{task.fields[detail]}"),
            TimeElapsedColumn(),
            console=console,
            expand=True,
        )
        self.summary_data = Progress(
            TextColumn("[bold]{task.description}", table_column=Column(width=10, no_wrap=True)),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TextColumn("ETA"),
            TimeRemainingColumn(),
            console=console,
            expand=True,
            speed_estimate_period=60,
        )
        self.work = Progress(
            TextColumn("{task.description}", table_column=Column(width=44, no_wrap=True, overflow="ellipsis")),
            BarColumn(bar_width=None),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[note]}", table_column=Column(width=24, no_wrap=True, overflow="ellipsis")),
            console=console,
            expand=True,
            speed_estimate_period=30,
        )
        self.items_task = self.summary_items.add_task(label, total=n_items, detail="")
        self._data_total = total_bytes or None
        self.data_task = self.summary_data.add_task("data", total=self._data_total, completed=have_bytes)
        self._lock = threading.Lock()
        self._last: dict[TaskID, int] = {}
        self._counts: dict[TaskID, bool] = {}
        self.ok = self.failed = self.skipped = 0
        self.moved = 0
        self.live = Live(
            Group(self.summary_items, self.summary_data, Rule(style="dim"), self.work),
            console=console,
            refresh_per_second=4,
            transient=False,
        )

    def __enter__(self) -> SyncUI:
        self.live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.live.__exit__(*exc)

    def print(self, *args, **kw) -> None:
        self.console.print(*args, **kw)

    # -- items -----------------------------------------------------------------------
    def item_begin(self, label: str, total: int | None, completed: int = 0, counts_in_total: bool = True) -> TaskID:
        tid = self.work.add_task(label, total=total, completed=completed, note="")
        with self._lock:
            self._last[tid] = completed
            self._counts[tid] = counts_in_total and total is not None
        return tid

    def item_advance(self, tid: TaskID, n: int) -> None:
        self.work.advance(tid, n)
        with self._lock:
            self._last[tid] = self._last.get(tid, 0) + n
            if n > 0:
                self.moved += n
        self.summary_data.advance(self.data_task, n)

    def item_report(self, tid: TaskID, completed: int | None = None, total: object = ..., note: str | None = None) -> None:
        """Absolute progress update (used by git sources). total=... leaves the total unchanged."""
        kw = {}
        if total is not ...:
            kw["total"] = total
        if note is not None:
            kw["note"] = note
        if completed is not None:
            with self._lock:
                delta = completed - self._last.get(tid, 0)
                self._last[tid] = completed
                if delta > 0:
                    self.moved += delta
                counts = self._counts.get(tid, False)
            kw["completed"] = completed
            # with no known overall total (e.g. repos without a GitHub token) still show bytes moved
            if delta and (counts or self._data_total is None):
                self.summary_data.advance(self.data_task, delta)
        if kw:
            self.work.update(tid, **kw)

    def item_end(self, tid: TaskID, outcome: str, line: str) -> None:
        self.work.remove_task(tid)
        with self._lock:
            self._last.pop(tid, None)
            self._counts.pop(tid, None)
            if outcome == "ok":
                self.ok += 1
            elif outcome == "skipped":
                self.skipped += 1
            else:
                self.failed += 1
            detail = f"[green]{self.ok} ok[/]" + (f"  [red]{self.failed} failed[/]" if self.failed else "") + (
                f"  [yellow]{self.skipped} skipped[/]" if self.skipped else ""
            )
        self.summary_items.update(self.items_task, advance=1, detail=detail)
        self.console.print(line)

    # -- big files -------------------------------------------------------------------
    def file_begin(self, name: str, total: int | None, completed: int = 0) -> TaskID:
        return self.work.add_task(f"  └ {name}", total=total, completed=completed, note="")

    def file_advance(self, tid: TaskID, n: int) -> None:
        self.work.advance(tid, n)

    def file_end(self, tid: TaskID) -> None:
        self.work.remove_task(tid)
