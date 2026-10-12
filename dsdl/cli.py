"""Command line: dsdl {sync,plan,status,verify} {datasets,code,all}."""

from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console

from dsdl import __version__
from dsdl.config import ConfigError, load_config
from dsdl.sheet import SheetError

TASKS = {"datasets": ["datasets"], "code": ["code"], "all": ["datasets", "code"]}


def _common(p: argparse.ArgumentParser, task_default: str | None = None) -> None:
    if task_default:
        p.add_argument("task", nargs="?", default=task_default, choices=TASKS, help=f"which sheet task (default: {task_default})")
    else:
        p.add_argument("task", choices=TASKS, help="datasets (first sheet), code (repos), or all")
    p.add_argument("--only", nargs="+", metavar="KEY", help="restrict to item keys/ids, e.g. openneuro:ds004196 or ds004196 or 'osf:*'")
    p.add_argument("--exclude", nargs="+", metavar="KEY", default=[], help="skip these item keys (added to config exclude)")
    p.add_argument("--offline", action="store_true", help="use the cached copy of the sheet")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="dsdl",
        description="Download the datasets and code repos listed in the Inner Speech Google Sheet. "
        "Reruns resume where they left off and finish in seconds when everything is present.",
    )
    ap.add_argument("--config", help="path to config.yaml (default: ./config.yaml or $DSDL_CONFIG)")
    ap.add_argument("--version", action="version", version=f"dsdl {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="download / resume / update")
    _common(p)
    p.add_argument("--check-updates", action="store_true", help="check complete items for newer upstream versions")
    p.add_argument("--retry-failed", action="store_true", help="retry items marked unavailable (not found / gated / private)")

    p = sub.add_parser("plan", help="list remote files and show sizes + time estimate without downloading")
    _common(p)
    p.add_argument("--all", dest="show_all", action="store_true", help="show complete items too")
    p.add_argument("--check-updates", action="store_true")
    p.add_argument("--retry-failed", action="store_true")

    p = sub.add_parser("status", help="show what is downloaded (no network unless the sheet was never fetched)")
    _common(p, task_default="all")
    p.add_argument("--all", dest="show_all", action="store_true", help="show complete items too")
    p.add_argument("--online", action="store_true", help="re-fetch the sheet first")

    p = sub.add_parser("verify", help="check complete items on disk (sizes; --checksums to rehash)")
    _common(p, task_default="all")
    p.add_argument("--checksums", action="store_true", help="re-hash files that have published checksums (slow)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    try:
        cfg = load_config(args.config)
        offline = args.offline or (args.cmd in ("status", "verify") and not getattr(args, "online", False))
        from dsdl.tasks import Runner  # heavy imports after arg parsing

        runner = Runner(cfg, console, offline=offline)
    except ConfigError as e:
        console.print(f"[red]config error:[/] {e}")
        return 2
    tasks = TASKS[args.task]
    try:
        if args.cmd == "sync":
            rc = runner.sync(tasks, args.only, args.exclude, args.check_updates, args.retry_failed)
            if rc == 130:  # interrupted: state is saved, skip joining threads blocked in network reads
                runner.close()
                os._exit(130)
            return rc
        if args.cmd == "plan":
            return runner.plan(tasks, args.only, args.exclude, args.check_updates, args.retry_failed, args.show_all)
        if args.cmd == "status":
            return runner.status(tasks, args.only, args.exclude, args.show_all)
        if args.cmd == "verify":
            return runner.verify(tasks, args.only, args.exclude, args.checksums)
    except SheetError as e:
        return _fail(console, e)
    except KeyboardInterrupt:
        # state is committed; don't wait for worker threads stuck in long network reads
        runner.cancel.set()
        runner.state.close()
        console.print("[yellow]interrupted — progress saved, rerun to resume[/]")
        os._exit(130)
    finally:
        runner.close()
    return 0


def _fail(console: Console, e: Exception) -> int:
    console.print(f"[red]error:[/] {e}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
