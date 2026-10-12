"""Source interface + helpers shared by all sources."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from dsdl.config import Config
from dsdl.http import Http, RemoteFile
from dsdl.resolve import Item
from dsdl.state import State

_BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')


class SourceError(Exception):
    """A permanent, explainable failure for one item (not found, gated, private...)."""


@dataclass
class Listing:
    version: str | None
    files: list[RemoteFile]
    warnings: list[str] = field(default_factory=list)


@dataclass
class Context:
    cfg: Config
    http: Http
    state: State
    cancel: threading.Event

    def item_dir(self, item: Item) -> Path:
        return self.cfg.output_dir / item.rel_dir


def safe_relpath(p: str) -> str:
    """Make a remote path safe to join under an item directory (no traversal, exFAT-safe names)."""
    parts = []
    for comp in p.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise SourceError(f"refusing unsafe remote path: {p!r}")
        comp = _BAD_CHARS.sub("_", comp).rstrip(" ")
        parts.append(comp or "_")
    if not parts:
        raise SourceError(f"empty remote path: {p!r}")
    return "/".join(parts)


class FileListSource:
    """Sources whose content is a list of files fetched over HTTP."""

    name = ""
    kind = "files"

    def list(self, item: Item, ctx: Context) -> Listing:
        raise NotImplementedError

    def current_version(self, item: Item, ctx: Context) -> str | None:
        """Cheap upstream version check for --check-updates; None = always re-list."""
        return None

    def listing_fingerprint(self, ctx: Context) -> str:
        """Config that shapes the listing; when it changes, cached listings are refreshed."""
        return ""


class GitSource:
    """Sources synced with git (GitHub, GIN/git-annex)."""

    name = ""
    kind = "git"

    def estimate(self, item: Item, ctx: Context) -> int | None:
        """Best-effort total size in bytes (may cache metadata in item meta)."""
        return None

    def sync(self, item: Item, ctx: Context, report, check_updates: bool) -> str | None:
        """Bring the local copy up to date. Returns a warning string or None. Raises on failure.

        report(completed=..., total=..., note=...) updates the progress display.
        """
        raise NotImplementedError
