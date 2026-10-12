"""Orchestration: sheet → items → listing/estimates → plan → downloads → summary."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from dsdl.config import Config, ensure_output_dir
from dsdl.http import Cancelled, Http, RemoteFile, make_hasher
from dsdl.progress import BIG_FILE, SyncUI, fmt_bytes, fmt_duration
from dsdl.report import Row, print_items, print_totals, write_index, write_manual
from dsdl.resolve import Item, Resolution, match_keys, resolve
from dsdl.sheet import fetch_sheet
from dsdl.sources import SOURCES, Context, SourceError
from dsdl.state import COMPLETE, FAILED, LISTED, PENDING, UNAVAILABLE, State

log = logging.getLogger("dsdl")

TOKEN_FOR_SOURCE = {"osf": "osf", "zenodo": "zenodo", "figshare": "figshare", "hf": "hf", "github": "github"}


class Runner:
    def __init__(self, cfg: Config, console: Console, offline: bool = False) -> None:
        ensure_output_dir(cfg.output_dir)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.console = console
        self.offline = offline
        self.cancel = threading.Event()
        self.state = State(cfg.state_dir / "state.sqlite")
        self.http = Http(cfg, self.cancel)
        self.ctx = Context(cfg, self.http, self.state, self.cancel)
        self.resolution: Resolution | None = None
        self.sheet_source = ""
        self.log_path = self._setup_logging()

    def close(self) -> None:
        self.http.close()
        self.state.close()

    def _setup_logging(self):
        logdir = self.cfg.state_dir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        old = sorted(logdir.glob("dsdl-*.log"))
        for f in old[:-30]:
            f.unlink(missing_ok=True)
        path = logdir / time.strftime("dsdl-%Y%m%d-%H%M%S.log")
        fh = logging.FileHandler(path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(fh)
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        return path

    # -- selection --------------------------------------------------------------------
    def load(self, tasks: list[str], only: list[str] | None = None, exclude: list[str] | None = None) -> list[Item]:
        tabs, self.sheet_source = fetch_sheet(self.cfg.sheet_id, self.cfg.state_dir, self.http.client, self.offline)
        res = self.resolution = resolve(tabs, self.cfg.datasets_tab, self.cfg.code_tab, self.cfg.include_dataset_code_column)
        items = [i for i in res.items if i.task in tasks]
        only = only or self.cfg.only
        if only:
            items = match_keys(items, only)
            for pat in only:
                if not match_keys([i for i in res.items if i.task in tasks], [pat]):
                    self.console.print(f"[yellow]⚠ --only {pat!r} matched nothing[/]")
        excl = [*self.cfg.exclude, *(exclude or [])]
        if excl:
            drop = {i.key for i in match_keys(items, excl)}
            items = [i for i in items if i.key not in drop]
        for it in items:
            self.state.upsert_item(it.key, it.task, it.source, it.url, it.rel_dir)
        self.state.commit()
        self._write_reports(tasks)
        return items

    def _write_reports(self, tasks: list[str]) -> None:
        assert self.resolution
        for t in tasks:
            write_index(self.cfg.output_dir, t, self.resolution.for_task(t), self.state)
        write_manual(self.cfg.output_dir, self.resolution)

    def _auth_fp(self, it: Item) -> str:
        tok = TOKEN_FOR_SOURCE.get(it.source)
        raw = json.dumps([bool(tok and self.cfg.token(tok)), self.cfg.override(it.key)], sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def _needs_work(self, it: Item, check_updates: bool, retry_failed: bool) -> bool:
        st = self.state.item(it.key)
        if st.status == COMPLETE:
            if not (self.cfg.output_dir / it.rel_dir).exists():
                self.state.reset_files(it.key)
                self.state.set_status(it.key, LISTED if self.state.has_manifest(it.key) else PENDING,
                                      error="local copy missing — downloading again")
                return True
            if check_updates or self._fingerprint_changed(it):
                return True
            return SOURCES[it.source].kind == "git" and self.cfg.git_update_on_rerun
        if st.status == UNAVAILABLE:
            return retry_failed or st.meta.get("auth_fp") != self._auth_fp(it)
        return True

    def _fingerprint_changed(self, it: Item) -> bool:
        src = SOURCES[it.source]
        if src.kind != "files" or not self.state.has_manifest(it.key):
            return False
        return self.state.item(it.key).meta.get("listing_fp", "") != src.listing_fingerprint(self.ctx)

    # -- listing / estimates ------------------------------------------------------------
    def prepare(self, items: list[Item], check_updates: bool, retry_failed: bool) -> list[Item]:
        """Resolve remote file lists (cached) for items that need work. Returns those items."""
        todo = [it for it in items if self._needs_work(it, check_updates, retry_failed)]
        need = []
        for it in todo:
            st = self.state.item(it.key)
            if (SOURCES[it.source].kind == "git" or check_updates or not self.state.has_manifest(it.key)
                    or st.status == UNAVAILABLE or self._fingerprint_changed(it)):
                need.append(it)
        if not need:
            return todo
        cols = (SpinnerColumn(), TextColumn("[bold]listing remote files"), BarColumn(), MofNCompleteColumn(),
                TimeElapsedColumn(), TextColumn("[dim]{task.fields[cur]}"))
        pool = ThreadPoolExecutor(8, thread_name_prefix="list")
        try:
            with Progress(*cols, console=self.console, transient=True) as prog:
                task = prog.add_task("list", total=len(need), cur="")
                futs = {pool.submit(self._list_one, it, check_updates): it for it in need}
                pending = set(futs)
                while pending:
                    done, pending = wait(pending, timeout=0.5)
                    for f in done:
                        prog.update(task, advance=1, cur=futs[f].key)
                        msg = f.result()
                        if msg:
                            self.console.print(msg)
        except KeyboardInterrupt:
            self.cancel.set()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return todo

    def _list_one(self, it: Item, check_updates: bool) -> str | None:
        src = SOURCES[it.source]
        key = it.key
        try:
            if src.kind == "git":
                src.estimate(it, self.ctx)
                if self.state.item(key).status == UNAVAILABLE:
                    self.state.set_status(key, PENDING)
                return None
            st = self.state.item(key)
            if check_updates and self.state.has_manifest(key) and st.version and not self._fingerprint_changed(it):
                v = src.current_version(it, self.ctx)
                if v is not None and v == st.version:
                    return None
                if st.status == COMPLETE:
                    self.console.print(f"[cyan]↻ {key}: new version {v} (had {st.version})[/]")
            listing = src.list(it, self.ctx)
            self.state.set_manifest(key, listing.version, listing.files)
            self.state.update_meta(key, warning="; ".join(listing.warnings)[:500] or None,
                                   listing_fp=src.listing_fingerprint(self.ctx))
            ft = self.state.file_totals(key)
            if ft.files == 0:
                self.state.set_status(key, COMPLETE)
                self.state.update_meta(key, warning="no downloadable files")
            elif ft.done_files == ft.files:
                self.state.set_status(key, COMPLETE)
            return None
        except Cancelled:
            return None
        except SourceError as e:
            self.state.set_status(key, UNAVAILABLE, error=str(e))
            self.state.update_meta(key, auth_fp=self._auth_fp(it))
            return f"[magenta]✖ {key}[/]: {e}"
        except Exception as e:  # network trouble, unexpected API shape … retried next run
            log.exception("listing %s failed", key)
            self.state.set_status(key, FAILED, error=f"listing failed: {type(e).__name__}: {e}"[:500])
            return f"[red]✖ {key}[/]: listing failed: {e}"

    # -- rows for tables -----------------------------------------------------------------
    def row(self, it: Item) -> Row:
        st = self.state.item(it.key)
        warning = st.meta.get("warning")
        if SOURCES[it.source].kind == "git":
            est = st.meta.get("disk_bytes") if st.status == COMPLETE else None
            if est is None:
                kb = st.meta.get("gh_size_kb")
                est = st.meta.get("annex_total") or (kb * 1024 if kb is not None else None)
            have = (est or 0) if st.status == COMPLETE else 0
            return Row(it, st.status, est, 0, have, None, st.error, warning)
        if not self.state.has_manifest(it.key):
            return Row(it, st.status, None, 0, 0, None, st.error, warning)
        ft = self.state.file_totals(it.key)
        have = ft.done_bytes if st.status == COMPLETE else ft.done_bytes + self._partial_bytes(it)
        return Row(it, st.status, ft.total_bytes, ft.unknown, min(have, ft.total_bytes or have), ft.files, st.error, warning)

    def _partial_bytes(self, it: Item) -> int:
        base = self.ctx.item_dir(it)
        if not base.exists():
            return 0
        n = 0
        for f in self.state.files(it.key, pending_only=True):
            p = base / f["path"]
            try:
                s = p.stat().st_size
                if f["size"] is not None and s == f["size"]:
                    n += s
                    continue
            except FileNotFoundError:
                pass
            try:
                n += p.with_name(p.name + ".part").stat().st_size
            except FileNotFoundError:
                pass
        return n

    def _order(self, rows: list[Row]) -> list[Row]:
        if self.cfg.order == "sheet":
            return rows
        return sorted(rows, key=lambda r: (r.remaining is None, r.remaining or 0))

    def _free(self) -> int:
        return shutil.disk_usage(self.cfg.output_dir).free

    # -- commands ------------------------------------------------------------------------
    def plan(self, tasks, only=None, exclude=None, check_updates=False, retry_failed=False, show_all=False) -> int:
        items = self.load(tasks, only, exclude)
        self._sheet_note()
        self.prepare(items, check_updates, retry_failed)
        rows = [self.row(i) for i in items]
        for task in tasks:
            trs = [r for r in rows if r.item.task == task]
            if not trs:
                continue
            todo = [r for r in trs if r.status != COMPLETE]
            print_items(self.console, f"{task}: {'all items' if show_all else 'to download'} (download order)",
                        self._order(trs if show_all else todo))
            print_totals(self.console, f"{task}: totals", trs, self._free(), self.state.avg_speed(), self._min_free())
            self._manual_note(task)
        return 0

    def status(self, tasks, only=None, exclude=None, show_all=False) -> int:
        items = self.load(tasks, only, exclude)
        self._sheet_note()
        rows = [self.row(i) for i in items]
        for task in tasks:
            trs = [r for r in rows if r.item.task == task]
            if not trs:
                continue
            shown = trs if show_all else [r for r in trs if r.status != COMPLETE or r.warning]
            if shown:
                print_items(self.console, f"{task}: {'all items' if show_all else 'not complete / with warnings'}", self._order(shown))
            print_totals(self.console, f"{task}: totals", trs, self._free(), self.state.avg_speed(), self._min_free())
            self._manual_note(task)
        return 0

    def sync(self, tasks, only=None, exclude=None, check_updates=False, retry_failed=False) -> int:
        t0 = time.time()
        items = self.load(tasks, only, exclude)
        self._sheet_note()
        if not items:
            self.console.print("nothing selected")
            return 0
        todo = self.prepare(items, check_updates, retry_failed)
        todo_keys = {i.key for i in todo}
        rows = [self.row(i) for i in items]
        is_git = lambda r: SOURCES[r.item.source].kind == "git"  # noqa: E731
        work = [
            r for r in rows
            if r.item.key in todo_keys and r.status != UNAVAILABLE
            and (is_git(r) or r.files is not None)  # file items need a listing first
            and (r.status != COMPLETE or (is_git(r) and (check_updates or self.cfg.git_update_on_rerun)))
        ]
        if not work:
            self._final_summary(tasks, rows, time.time() - t0)
            return 1 if any(r.status == FAILED for r in rows) else 0

        label = "+".join(tasks)
        print_totals(self.console, f"{label}: work for this run", work, self._free(), self.state.avg_speed(), self._min_free())
        ok, failed, skipped, interrupted, moved, elapsed = self._download(self._order(work), label, check_updates)
        self._write_reports(tasks)
        rows = [self.row(i) for i in items]
        speed = f" ({fmt_bytes(moved / elapsed)}/s)" if elapsed > 1 and moved else ""
        self.console.print(
            f"\n[bold]{'Interrupted' if interrupted else 'Finished'}[/] after {fmt_duration(elapsed)} — "
            f"downloaded {fmt_bytes(moved)}{speed} · {ok} done · {failed} failed · {skipped} skipped"
        )
        self._final_summary(tasks, rows, None)
        if interrupted:
            self.console.print("[yellow]Progress is saved — run the same command again to resume.[/]")
            return 130
        return 1 if failed else 0

    def verify(self, tasks, only=None, exclude=None, checksums=False) -> int:
        items = self.load(tasks, only, exclude)
        bad_items = 0
        targets = [it for it in items if self.state.item(it.key).status == COMPLETE]
        total_bytes = sum(self.row(it).total or 0 for it in targets if SOURCES[it.source].kind == "files") if checksums else None
        cols = (TextColumn("[bold]verifying"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(), TextColumn("[dim]{task.fields[cur]}"))
        with Progress(*cols, console=self.console) as prog:
            task = prog.add_task("verify", total=len(targets), cur="")
            for it in targets:
                prog.update(task, cur=it.key + (f" ({fmt_bytes(total_bytes)} to hash)" if checksums and total_bytes else ""))
                problem = self._verify_item(it, checksums)
                if problem:
                    bad_items += 1
                    prog.console.print(f"[red]✖ {it.key}[/]: {problem}")
                prog.advance(task)
        self.state.commit()
        msg = f"{len(targets)} complete items checked — " + ("[green]all good[/]" if not bad_items else f"[red]{bad_items} need re-download (run sync)[/]")
        self.console.print(msg)
        return 1 if bad_items else 0

    def _verify_item(self, it: Item, checksums: bool) -> str | None:
        base = self.ctx.item_dir(it)
        if SOURCES[it.source].kind == "git":
            if not (base / ".git").exists():
                self.state.set_status(it.key, PENDING, error="verify: checkout missing")
                return "checkout missing"
            if it.source == "gin":
                from dsdl.sources.gitrepo import _annex_bytes
                missing = _annex_bytes(base, here=False)
                if missing:
                    self.state.set_status(it.key, LISTED, error=f"verify: {fmt_bytes(missing)} of annexed content missing")
                    return f"{fmt_bytes(missing)} of annexed content missing"
            return None
        bad = []
        for f in self.state.files(it.key):
            p = base / f["path"]
            try:
                size = p.stat().st_size
            except FileNotFoundError:
                bad.append((f["path"], "missing"))
                continue
            if f["size"] is not None and size != f["size"]:
                bad.append((f["path"], f"size {size} != {f['size']}"))
            elif checksums and f["checksum"]:
                h = make_hasher(f["algo"], f["size"])
                if h is not None:
                    with open(p, "rb") as fh:
                        while block := fh.read(8 << 20):
                            h.update(block)
                    if h.hexdigest().lower() != f["checksum"].lower():
                        bad.append((f["path"], f"{f['algo']} mismatch"))
        for path, _ in bad:
            self.state.mark_file(it.key, path, done=False)
        if bad:
            self.state.set_status(it.key, LISTED, error=f"verify: {len(bad)} file(s) bad, e.g. {bad[0][0]} ({bad[0][1]})")
            return f"{len(bad)} file(s) bad, e.g. {bad[0][0]} ({bad[0][1]})"
        return None

    # -- downloading ---------------------------------------------------------------------
    def _download(self, rows: list[Row], label: str, check_updates: bool):
        file_rows = [r for r in rows if SOURCES[r.item.source].kind == "files"]
        git_rows = [r for r in rows if SOURCES[r.item.source].kind == "git"]
        total = sum(r.total for r in rows if r.total is not None)
        have = sum(r.have for r in rows)
        caffeinate = self._prevent_sleep()
        started = time.time()
        interrupted = False
        ui = SyncUI(self.console, label, len(rows), total or None, have)
        file_pool = ThreadPoolExecutor(self.cfg.parallel_files, thread_name_prefix="file")
        item_pool = ThreadPoolExecutor(self.cfg.parallel_items, thread_name_prefix="item")
        git_pool = ThreadPoolExecutor(self.cfg.git_parallel, thread_name_prefix="git")
        try:
            with ui:
                futs = [item_pool.submit(self._run_files_item, r, ui, file_pool) for r in file_rows]
                futs += [git_pool.submit(self._run_git_item, r, ui, check_updates) for r in git_rows]
                pending = set(futs)
                try:
                    while pending:
                        done, pending = wait(pending, timeout=0.5)
                        for f in done:
                            if f.exception() and not isinstance(f.exception(), Cancelled):
                                log.error("worker crashed", exc_info=f.exception())
                                ui.print(f"[red]internal error: {f.exception()!r} (see log)[/]")
                except KeyboardInterrupt:
                    interrupted = True
                    self.cancel.set()
                    ui.print("[yellow]Interrupted — stopping workers; partial files are kept…[/]")
                    try:
                        wait(futs, timeout=90)
                    except KeyboardInterrupt:
                        self.state.commit()
                        os._exit(130)
        finally:
            for p in (item_pool, git_pool, file_pool):
                p.shutdown(wait=False, cancel_futures=True)
            self.state.commit()
            if caffeinate:
                caffeinate.terminate()
        elapsed = time.time() - started
        if ui.moved > 0:
            self.state.record_run(label, started, elapsed, ui.moved)
        return ui.ok, ui.failed, ui.skipped, interrupted, ui.moved, elapsed

    def _run_files_item(self, row: Row, ui: SyncUI, file_pool: ThreadPoolExecutor) -> None:
        it, key = row.item, row.item.key
        if self.cancel.is_set():
            return
        min_free = self._min_free()
        free = self._free()
        if (row.remaining or 0) > free - min_free:
            msg = f"skipped: needs {fmt_bytes(row.remaining)}, only {fmt_bytes(max(0, free - min_free))} usable"
            self.state.set_status(key, LISTED, error=msg)
            ui.item_end(ui.item_begin(key, row.total), "skipped", f"[yellow]⏭ {key}[/] {msg}")
            return
        tid = ui.item_begin(key, row.total, row.have)
        base = self.ctx.item_dir(it)
        errors: list[tuple[str, Exception]] = []
        lock = threading.Lock()
        sem = threading.Semaphore(self.cfg.parallel_files)

        def one(f) -> None:
            try:
                rf = RemoteFile(f["path"], f["size"], f["url"], f["algo"], f["checksum"])
                dest = base / rf.path
                if rf.size is not None and dest.is_file() and dest.stat().st_size == rf.size:
                    self.state.mark_file(key, rf.path)  # already on disk (e.g. state was reset)
                    return
                fid = None
                if rf.size and rf.size >= BIG_FILE:
                    part = dest.with_name(dest.name + ".part")
                    fid = ui.file_begin(rf.path, rf.size, part.stat().st_size if part.exists() else 0)

                def on_bytes(n: int) -> None:
                    ui.item_advance(tid, n)
                    if fid is not None:
                        ui.file_advance(fid, n)

                try:
                    self.http.download(rf, dest, on_bytes, verify=self.cfg.verify_checksums)
                finally:
                    if fid is not None:
                        ui.file_end(fid)
                self.state.mark_file(key, rf.path)
            except Cancelled:
                pass
            except Exception as e:
                log.warning("%s: %s failed: %s", key, f["path"], e)
                with lock:
                    errors.append((f["path"], e))
            finally:
                sem.release()

        futs = []
        for f in self.state.files(key, pending_only=True):
            acquired = False
            while not acquired and not self.cancel.is_set():
                acquired = sem.acquire(timeout=0.5)
            if not acquired:
                break
            futs.append(file_pool.submit(one, f))
        wait(futs)
        self.state.commit()
        if self.cancel.is_set():
            ui.item_end(tid, "skipped", f"[yellow]⏸ {key}[/] interrupted")
            return
        ft = self.state.file_totals(key)
        if errors:
            path, e = errors[0]
            msg = f"{len(errors)} file(s) failed, e.g. {path}: {e}"
            self.state.set_status(key, FAILED, error=msg[:500])
            ui.item_end(tid, "failed", f"[red]✖ {key}[/] {msg[:200]}")
        elif ft.done_files == ft.files:
            self.state.set_status(key, COMPLETE)
            ui.item_end(tid, "ok", f"[green]✔ {key}[/]  {fmt_bytes(ft.total_bytes)} · {ft.files} files")
        else:
            self.state.set_status(key, FAILED, error=f"{ft.files - ft.done_files} file(s) not downloaded")
            ui.item_end(tid, "failed", f"[red]✖ {key}[/] {ft.files - ft.done_files} file(s) not downloaded")

    def _run_git_item(self, row: Row, ui: SyncUI, check_updates: bool) -> None:
        it, key = row.item, row.item.key
        if self.cancel.is_set():
            return
        src = SOURCES[it.source]
        tid = ui.item_begin(key, row.total if row.status != COMPLETE else None, 0)

        def report(completed=None, total=..., note=None):
            ui.item_report(tid, completed, total, note)

        try:
            warn = src.sync(it, self.ctx, report, check_updates or self.cfg.git_update_on_rerun)
        except Cancelled:
            ui.item_end(tid, "skipped", f"[yellow]⏸ {key}[/] interrupted")
            return
        except SourceError as e:
            self.state.set_status(key, UNAVAILABLE, error=str(e)[:500])
            self.state.update_meta(key, auth_fp=self._auth_fp(it))
            ui.item_end(tid, "failed", f"[magenta]✖ {key}[/] {e}")
            return
        except Exception as e:
            log.exception("%s failed", key)
            self.state.set_status(key, FAILED, error=f"{type(e).__name__}: {e}"[:500])
            ui.item_end(tid, "failed", f"[red]✖ {key}[/] {e}")
            return
        disk = _dir_size(self.ctx.item_dir(it))
        self.state.set_status(key, COMPLETE)
        self.state.update_meta(key, warning=warn, disk_bytes=disk)
        ui.item_end(tid, "ok", f"[green]✔ {key}[/]  {fmt_bytes(disk)}" + (f"  [yellow]⚠ {warn}[/]" if warn else ""))

    # -- misc ----------------------------------------------------------------------------
    def _min_free(self) -> int:
        return int(self.cfg.min_free_space_gb * 1e9)

    def _prevent_sleep(self):
        if self.cfg.prevent_sleep and sys.platform == "darwin" and shutil.which("caffeinate"):
            return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None

    def _sheet_note(self) -> None:
        note = f"sheet: {self.sheet_source}"
        self.console.print(f"[dim]{note} · output: {self.cfg.output_dir}[/]")

    def _manual_note(self, task: str) -> None:
        n = sum(1 for m in (self.resolution.manual if self.resolution else []) if m.task == task)
        if n:
            self.console.print(f"  [dim]{n} {task} entries need a manual download — see {self.cfg.output_dir / 'MANUAL_DOWNLOADS.md'}[/]")

    def _final_summary(self, tasks: list[str], rows: list[Row], elapsed: float | None) -> None:
        for task in tasks:
            trs = [r for r in rows if r.item.task == task]
            if not trs:
                continue
            n_ok = sum(r.status == COMPLETE for r in trs)
            size = sum(r.have for r in trs)
            parts = [f"{n_ok}/{len(trs)} complete ({fmt_bytes(size)} on disk)"]
            for st, style in ((UNAVAILABLE, "magenta"), (FAILED, "red")):
                n = sum(r.status == st for r in trs)
                if n:
                    parts.append(f"[{style}]{n} {st}[/]")
            other = sum(r.status in (PENDING, LISTED) for r in trs)
            if other:
                parts.append(f"[yellow]{other} incomplete[/]")
            n_manual = sum(1 for m in (self.resolution.manual if self.resolution else []) if m.task == task)
            if n_manual:
                parts.append(f"{n_manual} manual")
            mark = "[green]✔[/]" if n_ok == len(trs) else "[yellow]•[/]"
            tail = " — nothing to do" if elapsed is not None else ""
            self.console.print(f"{mark} [bold]{task}[/]: " + " · ".join(parts) + tail)
            for r in trs:
                if r.status in (FAILED, UNAVAILABLE) or (r.status in (PENDING, LISTED) and r.error):
                    self.console.print(f"    [dim]{r.item.key}[/]: {r.error}")
        if elapsed is not None:
            self.console.print(f"[dim]checked in {elapsed:.1f}s · `dsdl status` for details · log {self.log_path}[/]")


def _dir_size(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total
