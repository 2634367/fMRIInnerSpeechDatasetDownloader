"""Tables printed to the terminal and index files written next to the data."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from dsdl.progress import fmt_bytes, fmt_duration
from dsdl.resolve import Item, Resolution
from dsdl.state import COMPLETE, FAILED, UNAVAILABLE, State

STATUS_STYLE = {
    COMPLETE: "[green]complete[/]",
    FAILED: "[red]failed[/]",
    UNAVAILABLE: "[magenta]unavailable[/]",
    "listed": "[yellow]partial[/]",
    "pending": "[dim]pending[/]",
}
REFERENCE_SPEEDS = [(25e6, "25 MB/s"), (100e6, "100 MB/s")]


@dataclass
class Row:
    item: Item
    status: str
    total: int | None       # known bytes (None = unknown)
    unknown_files: int      # files whose size the source didn't report
    have: int               # bytes already on disk
    files: int | None
    error: str | None = None
    warning: str | None = None

    @property
    def remaining(self) -> int | None:
        return None if self.total is None else max(0, self.total - self.have)


def _eta_text(remaining: int, speed: float | None) -> str:
    if remaining <= 0:
        return "nothing left"
    if speed:
        return f"≈ {fmt_duration(remaining / speed)} at your measured {fmt_bytes(speed)}/s"
    return "  ·  ".join(f"≈ {fmt_duration(remaining / s)} at {label}" for s, label in REFERENCE_SPEEDS) + "  (no speed history yet)"


def print_totals(console: Console, title: str, rows: list[Row], free: int, speed: float | None, min_free: int) -> None:
    rows = [r for r in rows if r.status != UNAVAILABLE]
    by_src: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_src[r.item.source].append(r)
    t = Table(title=title, title_justify="left", show_footer=True, header_style="bold")
    t.add_column("source", footer="total")
    t.add_column("items", justify="right", footer=str(len(rows)))
    t.add_column("done", justify="right", footer=str(sum(r.status == COMPLETE for r in rows)))
    t.add_column("size", justify="right", footer=_size(sum(r.total or 0 for r in rows), any(r.total is None for r in rows)))
    t.add_column("on disk", justify="right", footer=fmt_bytes(sum(r.have for r in rows)))
    remaining = sum(r.remaining or 0 for r in rows)
    t.add_column("to download", justify="right", footer=_size(remaining, any(r.remaining is None for r in rows)))
    for src, rs in sorted(by_src.items()):
        t.add_row(
            src, str(len(rs)), str(sum(r.status == COMPLETE for r in rs)),
            _size(sum(r.total or 0 for r in rs), any(r.total is None for r in rs)),
            fmt_bytes(sum(r.have for r in rs)),
            _size(sum(r.remaining or 0 for r in rs), any(r.remaining is None for r in rs)),
        )
    console.print(t)
    console.print(f"  free space on output drive: [bold]{fmt_bytes(free)}[/]  (keeping {fmt_bytes(min_free)} free)")
    console.print(f"  estimated time: {_eta_text(remaining, speed)}")
    if remaining > free - min_free:
        console.print(
            f"  [yellow]⚠ {fmt_bytes(remaining)} to download but only {fmt_bytes(max(0, free - min_free))} usable — "
            "items are downloaded smallest-first and the ones that don't fit are skipped.[/]"
        )


def _status(r: Row) -> str:
    if r.status == "listed" and r.have == 0 and not r.error:
        return STATUS_STYLE["pending"]
    return STATUS_STYLE.get(r.status, r.status)


def _size(n: int, unknown: bool) -> str:
    return fmt_bytes(n) + (" +?" if unknown else "")


def print_items(console: Console, title: str, rows: list[Row], show_errors: bool = True) -> None:
    t = Table(title=title, title_justify="left", header_style="bold", expand=False)
    t.add_column("key", no_wrap=True, overflow="ellipsis", max_width=48)
    t.add_column("status", no_wrap=True)
    t.add_column("files", justify="right", no_wrap=True)
    t.add_column("size", justify="right", no_wrap=True)
    t.add_column("on disk", justify="right", no_wrap=True)
    t.add_column("left", justify="right", no_wrap=True)
    t.add_column("title / note", overflow="ellipsis", no_wrap=True, max_width=60)
    for r in rows:
        note = r.item.title
        if show_errors and (r.error or r.warning):
            note = f"[red]{r.error}[/]" if r.error else f"[yellow]⚠ {r.warning}[/]"
        t.add_row(
            r.item.key, _status(r),
            "?" if r.files is None else str(r.files),
            _size(r.total or 0, r.unknown_files > 0) if r.total is not None else "?",
            fmt_bytes(r.have), "?" if r.remaining is None else fmt_bytes(r.remaining), note,
        )
    console.print(t)


def write_index(output_dir: Path, task: str, items: list[Item], state: State) -> Path:
    path = output_dir / task / "INDEX.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "status", "source", "url", "local_path", "sheet_tab", "sheet_row", "column", "title", "categories", "notes"])
        for it in items:
            st = state.item(it.key)
            for ref in it.refs:
                w.writerow([it.key, st.status if st else "pending", it.source, it.url, it.rel_dir, ref.tab, ref.row,
                            ref.column, ref.title, ref.categories, ref.notes])
    tmp.replace(path)
    return path


def write_manual(output_dir: Path, resolution: Resolution) -> Path:
    path = output_dir / "MANUAL_DOWNLOADS.md"
    lines = [
        "# Manual downloads",
        "",
        "Entries from the sheet that dsdl can't fetch automatically (registration, data-use agreements,",
        "unsupported hosts, or no link). Regenerated on every run.",
        "",
    ]
    for task in ("datasets", "code"):
        ms = [m for m in resolution.manual if m.task == task]
        if not ms:
            continue
        lines += [f"## {task} ({len(ms)})", "", "| sheet row | title | link | why |", "|---|---|---|---|"]
        for m in ms:
            title = m.title.replace("|", "/")
            link = f"<{m.url}>" if m.url else "—"
            lines.append(f"| {m.tab} {m.row} | {title} | {link} | {m.reason} |")
        lines.append("")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)
    return path
