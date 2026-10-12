"""Resumable download behaviour against a local HTTP server."""

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dsdl.config import Config
from dsdl.http import Http, HttpError, RemoteFile, make_hasher
from dsdl.sources.base import SourceError, safe_relpath

DATA = bytes(range(256)) * 4096  # 1 MiB
MD5 = hashlib.md5(DATA).hexdigest()


class Handler(BaseHTTPRequestHandler):
    honour_range = True
    requests: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        Handler.requests.append((self.path, rng))
        if self.path == "/flaky" and len([r for r in Handler.requests if r[0] == "/flaky"]) == 1:
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        start = 0
        if rng and Handler.honour_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(DATA):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(DATA) - 1}/{len(DATA)}")
        else:
            self.send_response(200)
        body = DATA[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def server():
    Handler.requests = []
    Handler.honour_range = True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture()
def http(tmp_path):
    cfg = Config(path=tmp_path / "c.yaml", output_dir=tmp_path, state_dir=tmp_path / ".s", sheet_id="x", retries=3)
    h = Http(cfg)
    h._backoff = lambda attempt, retry_after: None  # no sleeping in tests
    yield h
    h.close()


def _counter():
    seen = []
    return seen, seen.append


def test_fresh_download_with_checksum(server, http, tmp_path):
    dest = tmp_path / "out" / "f.bin"
    seen, cb = _counter()
    moved = http.download(RemoteFile("f.bin", len(DATA), f"{server}/f", "md5", MD5), dest, cb)
    assert dest.read_bytes() == DATA and moved == len(DATA) and sum(seen) == len(DATA)
    assert not dest.with_name("f.bin.part").exists()


def test_resume_from_partial(server, http, tmp_path):
    dest = tmp_path / "f.bin"
    dest.with_name("f.bin.part").write_bytes(DATA[:300_000])
    seen, cb = _counter()
    moved = http.download(RemoteFile("f.bin", len(DATA), f"{server}/f", "md5", MD5), dest, cb)
    assert dest.read_bytes() == DATA
    assert moved == len(DATA) - 300_000 == sum(seen)
    assert Handler.requests[-1][1] == "bytes=300000-"


def test_server_ignoring_range_restarts(server, http, tmp_path):
    Handler.honour_range = False
    dest = tmp_path / "f.bin"
    dest.with_name("f.bin.part").write_bytes(DATA[:300_000])
    seen, cb = _counter()
    http.download(RemoteFile("f.bin", len(DATA), f"{server}/f", "md5", MD5), dest, cb)
    assert dest.read_bytes() == DATA
    assert sum(seen) == len(DATA) - 300_000  # the -300000 correction balances the full re-download


def test_complete_part_is_finalised_without_body(server, http, tmp_path):
    dest = tmp_path / "f.bin"
    dest.with_name("f.bin.part").write_bytes(DATA)
    http.download(RemoteFile("f.bin", len(DATA), f"{server}/f", "md5", MD5), dest, lambda n: None)
    assert dest.read_bytes() == DATA and Handler.requests == []


def test_checksum_mismatch_fails_after_one_retry(server, http, tmp_path):
    dest = tmp_path / "f.bin"
    with pytest.raises(HttpError, match="mismatch"):
        http.download(RemoteFile("f.bin", len(DATA), f"{server}/f", "md5", "0" * 32), dest, lambda n: None)
    assert not dest.exists() and len(Handler.requests) == 2


def test_retry_on_503(server, http, tmp_path):
    dest = tmp_path / "f.bin"
    http.download(RemoteFile("f.bin", len(DATA), f"{server}/flaky"), dest, lambda n: None)
    assert dest.read_bytes() == DATA and len(Handler.requests) == 2


def test_404_is_permanent(server, http, tmp_path):
    with pytest.raises(HttpError) as e:
        http.download(RemoteFile("f.bin", 10, f"{server}/missing"), tmp_path / "f.bin", lambda n: None)
    assert e.value.status == 404 and len(Handler.requests) == 1


def test_gitsha1_matches_git():
    h = make_hasher("gitsha1", 6)
    h.update(b"hello\n")
    assert h.hexdigest() == "ce013625030ba8dba906f756967f9e9ca394464a"  # `echo hello | git hash-object --stdin`


@pytest.mark.parametrize(
    "raw,safe",
    [("sub-01/anat/x.nii.gz", "sub-01/anat/x.nii.gz"), ("/a//b/./c", "a/b/c"), ('a:b/c?"d', "a_b/c__d"), ("dir /x", "dir/x")],
)
def test_safe_relpath(raw, safe):
    assert safe_relpath(raw) == safe


@pytest.mark.parametrize("raw", ["../etc/passwd", "a/../../b", "", "/"])
def test_safe_relpath_rejects(raw):
    with pytest.raises(SourceError):
        safe_relpath(raw)


def test_no_params_keeps_query(server, http):
    r = http.request("GET", f"{server}/f?page=2", params={"x": "1"})
    assert r.status_code == 200
    assert Handler.requests[-1][0] == "/f?page=2&x=1"
