"""Shared HTTP layer: per-host throttling, retries with backoff, resumable hashed downloads."""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from dsdl import USER_AGENT

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
CHUNK = 1 << 20

# host -> (max concurrent requests, min seconds between request starts)
HOST_POLICY: dict[str, tuple[int, float]] = {
    "zenodo.org": (3, 0.4),
    "api.osf.io": (4, 0.2),
    "osf.io": (4, 0.2),
    ".osf.io": (4, 0.0),
    "api.figshare.com": (4, 0.2),
    "ndownloader.figshare.com": (4, 0.0),
    "huggingface.co": (8, 0.02),
    "api.github.com": (4, 0.0),
    "openneuro.org": (4, 0.1),
}


class Cancelled(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status: int | None, url: str, detail: str = "") -> None:
        self.status = status
        self.url = url
        head = f"HTTP {status} for {url}" if status is not None else f"request to {url} failed"
        super().__init__(head + (f": {detail}" if detail else ""))


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


class _ChecksumMismatch(Exception):
    def __init__(self, counted: int, msg: str) -> None:
        super().__init__(msg)
        self.counted = counted


@dataclass
class RemoteFile:
    path: str  # sanitised, relative, posix
    size: int | None
    url: str
    algo: str | None = None  # md5 | sha1 | sha256 | gitsha1
    checksum: str | None = None


class _Gate:
    def __init__(self, concurrency: int, interval: float) -> None:
        self._sem = threading.BoundedSemaphore(concurrency)
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self, cancel: threading.Event) -> None:
        while not self._sem.acquire(timeout=0.5):
            if cancel.is_set():
                raise Cancelled()
        if self._interval:
            with self._lock:
                now = time.monotonic()
                wait = max(0.0, self._next - now)
                self._next = max(now, self._next) + self._interval
            if wait and cancel.wait(wait):
                self._sem.release()
                raise Cancelled()

    def release(self) -> None:
        self._sem.release()


def make_hasher(algo: str | None, size: int | None):
    if algo == "gitsha1":
        if size is None:
            return None
        h = hashlib.sha1()
        h.update(f"blob {size}\0".encode())
        return h
    if algo in ("md5", "sha1", "sha256", "sha512"):
        return hashlib.new(algo)
    return None


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class Http:
    def __init__(self, cfg, cancel: threading.Event | None = None) -> None:
        self.cfg = cfg
        self.cancel = cancel or threading.Event()
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=httpx.Timeout(cfg.timeout, connect=30.0),
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=64),
        )
        self._gates: dict[str, _Gate] = {}
        self._gates_lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    # -- policy -------------------------------------------------------------------
    def _gate(self, url: str) -> _Gate:
        host = (urlparse(url).hostname or "").lower()
        with self._gates_lock:
            g = self._gates.get(host)
            if g is None:
                pol = HOST_POLICY.get(host)
                if pol is None:
                    pol = next((v for k, v in HOST_POLICY.items() if k.startswith(".") and host.endswith(k)), None)
                conc, interval = pol or (self.cfg.parallel_files, 0.0)
                g = self._gates[host] = _Gate(conc, interval)
            return g

    def auth_headers(self, url: str) -> dict[str, str]:
        host = (urlparse(url).hostname or "").lower()
        t = self.cfg.tokens
        if host == "api.github.com" and t.get("github"):
            return {"Authorization": f"Bearer {t['github']}"}
        if host == "huggingface.co" and t.get("hf"):
            return {"Authorization": f"Bearer {t['hf']}"}
        if (host == "osf.io" or host.endswith(".osf.io")) and t.get("osf"):
            return {"Authorization": f"Bearer {t['osf']}"}
        if host == "zenodo.org" and t.get("zenodo"):
            return {"Authorization": f"Bearer {t['zenodo']}"}
        if host == "api.figshare.com" and t.get("figshare"):
            return {"Authorization": f"token {t['figshare']}"}
        return {}

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = min(120.0, 2.0 ** min(attempt, 7) + random.random())
        if retry_after is not None:
            delay = min(600.0, max(delay, retry_after))
        log.debug("backing off %.1fs (attempt %d)", delay, attempt)
        if self.cancel.wait(delay):
            raise Cancelled()

    # -- simple requests ------------------------------------------------------------
    def request(self, method: str, url: str, *, params=None, json=None, headers=None, timeout=None) -> httpx.Response:
        # httpx *replaces* a URL's query string when params is given — merge instead, so
        # pagination links like ...?page=2 keep their own parameters.
        if params:
            url = str(httpx.URL(url).copy_merge_params(params))
        attempt = 0
        while True:
            if self.cancel.is_set():
                raise Cancelled()
            gate = self._gate(url)
            gate.acquire(self.cancel)
            try:
                r = self.client.request(
                    method, url, json=json,
                    headers={**self.auth_headers(url), **(headers or {})},
                    timeout=timeout or httpx.USE_CLIENT_DEFAULT,
                )
                if r.status_code not in RETRY_STATUS:
                    return r
                err: Exception = _Retryable(f"HTTP {r.status_code}", _retry_after(r))
            except httpx.TransportError as e:
                err = e
            finally:
                gate.release()
            attempt += 1
            if attempt > self.cfg.retries:
                if isinstance(err, _Retryable):
                    raise HttpError(None, url, f"gave up after {attempt} attempts ({err})")
                raise HttpError(None, url, f"gave up after {attempt} attempts ({type(err).__name__}: {err})")
            log.info("retrying %s %s: %s", method, url, err)
            self._backoff(attempt, getattr(err, "retry_after", None))

    def get_json(self, url: str, params=None, headers=None, timeout=None) -> Any:
        r = self.request("GET", url, params=params, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            raise HttpError(r.status_code, url, r.text[:200].strip())
        return r.json()

    def post_json(self, url: str, payload: dict, timeout=None) -> Any:
        r = self.request("POST", url, json=payload, timeout=timeout)
        if r.status_code >= 400:
            raise HttpError(r.status_code, url, r.text[:200].strip())
        return r.json()

    # -- file downloads -------------------------------------------------------------
    def download(self, rf: RemoteFile, dest: Path, on_bytes: Callable[[int], None], verify: bool = True) -> int:
        """Download rf to dest (via dest.part, resuming). Returns bytes moved over the network.

        on_bytes(delta) is called as bytes arrive; it gets a negative delta when previously
        counted partial data has to be discarded (server ignored Range, checksum mismatch).
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        attempt, moved, rehashed = 0, [0], False
        while True:
            if self.cancel.is_set():
                raise Cancelled()
            try:
                self._download_once(rf, dest, part, on_bytes, verify, moved)
                return moved[0]
            except _ChecksumMismatch as e:
                part.unlink(missing_ok=True)
                on_bytes(-e.counted)
                if rehashed:
                    raise HttpError(None, rf.url, str(e)) from None
                rehashed = True
                log.warning("%s — downloading %s again from scratch", e, rf.path)
            except (_Retryable, httpx.TransportError) as e:
                attempt += 1
                if attempt > self.cfg.retries:
                    raise HttpError(None, rf.url, f"gave up after {attempt} attempts ({e})") from None
                log.info("retrying %s: %s", rf.path, e)
                self._backoff(attempt, getattr(e, "retry_after", None))

    def _download_once(self, rf: RemoteFile, dest: Path, part: Path, on_bytes, verify: bool, moved: list[int]) -> None:
        existing = part.stat().st_size if part.exists() else 0
        if rf.size is not None and existing > rf.size:
            part.unlink()
            on_bytes(-existing)
            existing = 0
        hasher = make_hasher(rf.algo, rf.size) if (verify and rf.checksum) else None
        if hasher and existing:
            with open(part, "rb") as fh:
                while block := fh.read(8 * CHUNK):
                    if self.cancel.is_set():
                        raise Cancelled()
                    hasher.update(block)
        if rf.size is not None and existing == rf.size and existing > 0:
            self._finalize(rf, dest, part, hasher, existing)
            return

        headers = {**self.auth_headers(rf.url), "Accept-Encoding": "identity"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        gate = self._gate(rf.url)
        gate.acquire(self.cancel)
        try:
            with self.client.stream("GET", rf.url, headers=headers) as r:
                if r.status_code == 416 and existing:
                    # nothing left to fetch: the partial file is already complete
                    self._finalize(rf, dest, part, hasher, existing)
                    return
                if r.status_code in RETRY_STATUS:
                    raise _Retryable(f"HTTP {r.status_code}", _retry_after(r))
                if r.status_code >= 400:
                    raise HttpError(r.status_code, rf.url, _short_error(r))
                mode = "ab"
                if existing and r.status_code != 206:
                    on_bytes(-existing)  # server ignored Range — start over
                    existing = 0
                    hasher = make_hasher(rf.algo, rf.size) if (verify and rf.checksum) else None
                if not existing:
                    mode = "wb"
                size = existing
                with open(part, mode) as fh:
                    for chunk in r.iter_bytes(CHUNK):
                        if self.cancel.is_set():
                            raise Cancelled()
                        fh.write(chunk)
                        if hasher:
                            hasher.update(chunk)
                        size += len(chunk)
                        moved[0] += len(chunk)
                        on_bytes(len(chunk))
        finally:
            gate.release()
        if rf.size is not None and size < rf.size:
            raise _Retryable(f"connection closed early ({size}/{rf.size} bytes)")
        self._finalize(rf, dest, part, hasher, size)

    @staticmethod
    def _finalize(rf: RemoteFile, dest: Path, part: Path, hasher, size: int) -> None:
        if rf.size is not None and size != rf.size:
            part.unlink(missing_ok=True)
            raise HttpError(None, rf.url, f"size mismatch: got {size}, expected {rf.size}")
        if hasher is not None and hasher.hexdigest().lower() != (rf.checksum or "").lower():
            raise _ChecksumMismatch(size, f"{rf.algo} mismatch for {rf.path}")
        os.replace(part, dest)


def _short_error(r: httpx.Response) -> str:
    try:
        r.read()
        return r.text[:200].strip()
    except Exception:
        return ""
