"""Fetch the Google Sheet as an HTML-zip export and parse every tab into rows.

The HTML export is used (not xlsx/csv) because it keeps *every* hyperlink inside a
cell — xlsx keeps only the first one, csv keeps none.
"""

from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=zip"


@dataclass
class Cell:
    text: str = ""
    links: list[str] = field(default_factory=list)


@dataclass
class Row:
    tab: str
    number: int  # 1-based spreadsheet row number
    cells: dict[str, Cell]  # keyed by lower-cased header name

    def text(self, col: str) -> str:
        c = self.cells.get(col.lower())
        return c.text if c else ""

    def links(self, col: str) -> list[str]:
        c = self.cells.get(col.lower())
        return list(c.links) if c else []


class SheetError(Exception):
    pass


def unwrap_google_redirect(url: str) -> str:
    p = urlparse(url)
    if p.netloc.endswith("google.com") and p.path == "/url":
        q = parse_qs(p.query).get("q")
        if q:
            return q[0]
    return url


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[int | None, list[Cell]]] = []
        self._in_tbody = False
        self._row: list[Cell] | None = None
        self._row_num: int | None = None
        self._cell: Cell | None = None
        self._colspan = 1
        self._in_th = False
        self._th_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tbody":
            self._in_tbody = True
        elif not self._in_tbody:
            return
        elif tag == "tr":
            self._row, self._row_num = [], None
        elif tag == "th" and self._row is not None:
            self._in_th, self._th_text = True, ""
        elif tag == "td" and self._row is not None:
            if "freezebar" in (a.get("class") or ""):
                return
            self._cell = Cell()
            try:
                self._colspan = max(1, int(a.get("colspan") or 1))
            except ValueError:
                self._colspan = 1
        elif tag == "a" and self._cell is not None:
            href = a.get("href")
            if href:
                href = unwrap_google_redirect(href.strip())
                if href not in self._cell.links:
                    self._cell.links.append(href)
        elif tag == "br" and self._cell is not None:
            self._cell.text += "\n"

    def handle_endtag(self, tag):
        if tag == "tbody":
            self._in_tbody = False
        elif tag == "th" and self._in_th:
            self._in_th = False
            if self._th_text.strip().isdigit():
                self._row_num = int(self._th_text.strip())
        elif tag == "td" and self._cell is not None and self._row is not None:
            lines = (" ".join(ln.split()) for ln in self._cell.text.split("\n"))
            self._cell.text = "\n".join(ln for ln in lines if ln)
            self._row.append(self._cell)
            self._row.extend(Cell() for _ in range(self._colspan - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append((self._row_num, self._row))
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data
        elif self._in_th:
            self._th_text += data


def parse_table_html(html: str, tab: str) -> list[Row]:
    p = _TableParser()
    p.feed(html)
    header: list[str] | None = None
    rows: list[Row] = []
    for i, (num, cells) in enumerate(p.rows, start=1):
        if header is None:
            if any(c.text for c in cells):
                header = [c.text.strip().lower() for c in cells]
            continue
        if not any(c.text or c.links for c in cells):
            continue
        named = {}
        for name, cell in zip(header, cells):
            if name and name not in named:
                named[name] = cell
        rows.append(Row(tab=tab, number=num or i, cells=named))
    return rows


def parse_zip(data: bytes) -> dict[str, list[Row]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SheetError("sheet export is not a zip — is the sheet shared as 'anyone with the link'?") from e
    tabs = {}
    for name in z.namelist():
        if name.lower().endswith(".html") and "/" not in name:
            tab = name[: -len(".html")]
            tabs[tab] = parse_table_html(z.read(name).decode("utf-8", "replace"), tab)
    if not tabs:
        raise SheetError("sheet export contained no tabs")
    return tabs


def fetch_sheet(sheet_id: str, cache_dir: Path, client, offline: bool = False) -> tuple[dict[str, list[Row]], str]:
    """Return (tabs, where they came from). offline=True prefers the cached copy; a failed fetch falls back to it."""
    cache = cache_dir / "sheet.zip"

    def cached(why: str):
        age = time.strftime("%Y-%m-%d %H:%M", time.localtime(cache.stat().st_mtime))
        return parse_zip(cache.read_bytes()), f"cached copy from {age} ({why})"

    if offline and cache.is_file():
        return cached("offline")
    try:
        r = client.get(EXPORT_URL.format(id=sheet_id), follow_redirects=True, timeout=60)
        r.raise_for_status()
        tabs = parse_zip(r.content)
    except Exception as e:  # network, HTTP, or parse failure → try the cache
        if cache.is_file():
            return cached(f"fetch failed: {e}")
        raise SheetError(f"could not fetch the sheet and no cached copy exists: {e}") from e
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(cache)
    return tabs, "live"
