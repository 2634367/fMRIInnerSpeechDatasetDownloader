"""Git-based sources: GitHub (shallow clone + LFS) and GIN (git-annex)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from dsdl.http import Cancelled, HttpError
from dsdl.resolve import Item
from dsdl.sources.base import Context, GitSource, SourceError

log = logging.getLogger(__name__)

_UNITS = {"B": 1, "bytes": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3}
_RECV = re.compile(r"Receiving objects:\s+(\d+)%[^,]*(?:,\s*([\d.]+)\s*(\w+))?")
_PERMANENT = (
    "Repository not found", "not found", "Authentication failed", "could not read Username",
    "terminal prompts disabled", "does not appear to be a git repository", "403",
)


class GitError(Exception):
    """A git/git-annex command failed (possibly transient — retried on the next run)."""


def _git_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_ASKPASS": "true", "LC_ALL": "C"})
    env.update(extra or {})
    return env


def _run(args: list[str], cwd: Path | None = None, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("run %s (cwd=%s)", args, cwd)
    p = subprocess.run(args, cwd=cwd, env=_git_env(), capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise GitError(f"{' '.join(args[:3])} failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def _stream_lines(proc: subprocess.Popen, stream, on_line, ctx: Context) -> str:
    """Read \\r- or \\n-separated progress lines from a pipe; returns the tail for error messages."""
    tail, buf = [], b""
    while True:
        if ctx.cancel.is_set():
            proc.terminate()
        chunk = stream.read1(4096)
        if not chunk:
            break
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                on_line(line)
                tail = (tail + [line])[-20:]
    proc.wait()
    return "\n".join(tail)


def _clone(url: str, dest: Path, depth: int, ctx: Context, report, est_bytes: int | None) -> None:
    """Clone into dest.partial, then rename — an interrupted clone never looks complete."""
    partial = dest.with_name(dest.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["git", "clone", "--progress", "--no-tags"]
    if depth > 0:
        args += ["--depth", str(depth), "--single-branch"]
    args += [url, str(partial)]

    def on_line(line: str) -> None:
        m = _RECV.search(line)
        if not m:
            return
        pct = int(m[1])
        got = int(float(m[2]) * _UNITS.get(m[3], 1)) if m[2] else None
        if est_bytes:
            report(completed=max(got or 0, est_bytes * pct // 100))
        else:
            report(completed=got or 0, note=f"{pct}%")

    for attempt in range(3):
        proc = subprocess.Popen(args, env=_git_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        tail = _stream_lines(proc, proc.stderr, on_line, ctx)
        if ctx.cancel.is_set():
            shutil.rmtree(partial, ignore_errors=True)
            raise Cancelled()
        if proc.returncode == 0:
            break
        shutil.rmtree(partial, ignore_errors=True)
        msg = tail.splitlines()[-1] if tail else f"exit {proc.returncode}"
        if any(s in tail for s in _PERMANENT):
            if "could not read Username" in tail or "terminal prompts disabled" in tail:
                msg = "repository not found or private"
            raise SourceError(f"git clone failed: {msg}")
        if attempt == 2:
            raise GitError(f"git clone failed: {msg}")
        time.sleep(5 * (attempt + 1))
    if dest.exists():
        if any(dest.iterdir()):
            raise SourceError(f"{dest} exists and is not a git checkout — move it away and rerun")
        dest.rmdir()
    os.replace(partial, dest)


class GitHub(GitSource):
    name = "github"

    def estimate(self, item: Item, ctx: Context) -> int | None:
        st = ctx.state.item(item.key)
        meta = st.meta if st else {}
        if "gh_size_kb" in meta:
            return meta["gh_size_kb"] * 1024
        if not ctx.cfg.token("github"):
            return None  # 60 req/h unauthenticated — don't burn it on estimates
        try:
            info = ctx.http.get_json(f"https://api.github.com/repos/{item.ident}")
        except HttpError as e:
            if e.status == 404:
                raise SourceError("repository not found or private") from e
            log.info("github api %s: %s", item.ident, e)
            return None
        ctx.state.update_meta(item.key, gh_size_kb=info.get("size") or 0, default_branch=info.get("default_branch"),
                              archived=info.get("archived", False))
        return (info.get("size") or 0) * 1024

    def sync(self, item: Item, ctx: Context, report, check_updates: bool) -> str | None:
        dest = ctx.item_dir(item)
        url = item.url + ".git"
        est = self.estimate(item, ctx)
        if not (dest / ".git").exists():
            report(total=est, note="cloning")
            _clone(url, dest, ctx.cfg.git_depth, ctx, report, est)
        elif check_updates:
            report(total=None, note="checking for updates")
            warn = self._update(url, dest, ctx)
            if warn:
                return warn
        if ctx.cfg.git_lfs:
            return self._lfs(dest, report)
        return None

    def _update(self, url: str, dest: Path, ctx: Context) -> str | None:
        remote = _run(["git", "ls-remote", url, "HEAD"], timeout=120).stdout.split()
        local = _run(["git", "rev-parse", "HEAD"], cwd=dest).stdout.strip()
        if not remote or remote[0] == local:
            return None
        if _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=dest).stdout.strip():
            return "upstream changed but local checkout has modifications — not updated"
        ahead = _run(["git", "rev-list", "--count", "HEAD", "--not", "--remotes"], cwd=dest, check=False).stdout.strip()
        if ahead not in ("", "0"):
            return "upstream changed but local checkout has its own commits — not updated"
        depth = ["--depth", str(ctx.cfg.git_depth)] if ctx.cfg.git_depth > 0 else []
        _run(["git", "fetch", *depth, "origin", "HEAD"], cwd=dest, timeout=3600)
        _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=dest)
        return None

    def _lfs(self, dest: Path, report) -> str | None:
        if not (dest / ".gitattributes").exists() or "filter=lfs" not in (dest / ".gitattributes").read_text(errors="replace"):
            return None
        if not shutil.which("git-lfs"):
            return "repo uses git-lfs but git-lfs is not installed — LFS files are pointers"
        sizes = _run(["git", "lfs", "ls-files", "--size"], cwd=dest, check=False).stdout.strip().splitlines()
        if not sizes:
            return None
        report(note=f"git lfs pull ({len(sizes)} files)")
        p = _run(["git", "lfs", "pull"], cwd=dest, timeout=24 * 3600, check=False)
        if p.returncode != 0:
            return "LFS files unavailable: " + (p.stderr or p.stdout).strip().splitlines()[-1][:200]
        return None


class Gin(GitSource):
    """GIN (gin.g-node.org) repos: git clone + git-annex get over HTTPS."""

    name = "gin"

    def _ensure_clone(self, item: Item, ctx: Context, report) -> Path:
        dest = ctx.item_dir(item)
        if not shutil.which("git-annex"):
            raise SourceError("git-annex is required for GIN repos (brew install git-annex)")
        if not (dest / ".git").exists():
            report(note="cloning metadata")
            _clone(item.url + ".git", dest, 0, ctx, lambda **kw: None, None)
            _run(["git", "annex", "init", "dsdl"], cwd=dest, timeout=600)
        return dest

    def estimate(self, item: Item, ctx: Context) -> int | None:
        st = ctx.state.item(item.key)
        if st and "annex_total" in st.meta:
            return st.meta["annex_total"]
        dest = self._ensure_clone(item, ctx, lambda **kw: None)
        total = _annex_bytes(dest, here=True) + _annex_bytes(dest, here=False)
        ctx.state.update_meta(item.key, annex_total=total)
        return total

    def sync(self, item: Item, ctx: Context, report, check_updates: bool) -> str | None:
        dest = self._ensure_clone(item, ctx, report)
        if check_updates:
            report(note="pulling updates")
            _run(["git", "pull", "--ff-only"], cwd=dest, timeout=3600, check=False)
        present, missing = _annex_bytes(dest, here=True), _annex_bytes(dest, here=False)
        ctx.state.update_meta(item.key, annex_total=present + missing)
        if missing == 0:
            return None
        report(total=present + missing, completed=present, note="git annex get")
        in_flight: dict[str, int] = {}
        finished = [0]

        def on_line(line: str) -> None:
            try:
                msg = json.loads(line)
            except ValueError:
                return
            f = (msg.get("action") or {}).get("file") or msg.get("file") or ""
            if "byte-progress" in msg:
                in_flight[f] = int(msg["byte-progress"])
            elif msg.get("command") == "get":
                in_flight.pop(f, None)
                if msg.get("success"):
                    finished[0] += _key_size(msg.get("key"))
            report(completed=present + finished[0] + sum(in_flight.values()))

        args = ["git", "annex", "get", f"-J{ctx.cfg.parallel_files}", "--json", "--json-progress", "."]
        proc = subprocess.Popen(args, cwd=dest, env=_git_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        tail = _stream_lines(proc, proc.stdout, on_line, ctx)
        if ctx.cancel.is_set():
            raise Cancelled()
        still = _annex_bytes(dest, here=False)
        if still:
            raise GitError(f"git annex get incomplete ({still} bytes still missing): {tail.splitlines()[-1][:200] if tail else ''}")
        return None


def _annex_bytes(dest: Path, here: bool) -> int:
    where = ["--in=here"] if here else ["--not", "--in=here"]
    out = _run(["git", "annex", "find", *where, "--format=${bytesize}\\n"], cwd=dest, timeout=3600).stdout
    return sum(int(x) for x in out.split() if x.isdigit())


def _key_size(key) -> int:
    m = re.search(r"-s(\d+)--", str(key or ""))
    return int(m[1]) if m else 0
