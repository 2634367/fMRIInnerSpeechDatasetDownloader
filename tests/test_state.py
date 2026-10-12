from dsdl.http import RemoteFile
from dsdl.state import COMPLETE, State


def test_manifest_merge_keeps_unchanged_done_files(tmp_path):
    s = State(tmp_path / "s.sqlite")
    s.upsert_item("x:1", "datasets", "x", "u", "datasets/x/1")
    s.set_manifest("x:1", "v1", [
        RemoteFile("a", 10, "http://h/a?v=1", "md5", "aa"),
        RemoteFile("b", 20, "http://h/b?v=1"),
        RemoteFile("c", 30, "http://h/c?v=1"),
    ])
    for p in ("a", "b", "c"):
        s.mark_file("x:1", p)
    s.set_status("x:1", COMPLETE)
    # new version: a unchanged (same checksum, new url), b's object changed, c removed, d added
    s.set_manifest("x:1", "v2", [
        RemoteFile("a", 10, "http://h/a?v=2", "md5", "aa"),
        RemoteFile("b", 20, "http://h/b?v=2"),
        RemoteFile("d", None, "http://h/d"),
    ])
    done = {r["path"]: r["done"] for r in s.files("x:1")}
    assert done == {"a": 1, "b": 0, "d": 0}
    t = s.file_totals("x:1")
    assert (t.files, t.total_bytes, t.unknown, t.done_files, t.done_bytes) == (3, 30, 1, 1, 10)
    it = s.item("x:1")
    assert it.status == "listed" and it.version == "v2" and it.total_bytes is None


def test_meta_and_speed(tmp_path):
    s = State(tmp_path / "s.sqlite")
    s.upsert_item("k", "code", "github", "u", "p")
    assert s.update_meta("k", a=1) == {"a": 1}
    assert s.update_meta("k", b=2) == {"a": 1, "b": 2}
    assert s.avg_speed() is None
    s.record_run("datasets", 0, 100.0, 1_000_000_000)
    assert s.avg_speed() == 10_000_000


def test_unique_paths_case_insensitive():
    from dsdl.state import unique_paths

    files = [RemoteFile("d/a.txt", 1, "u1"), RemoteFile("d/A.txt", 2, "u2"), RemoteFile("d/a.txt", 3, "u3"), RemoteFile("x", 1, "u4"), RemoteFile("x", 1, "u5")]
    assert [f.path for f in unique_paths(files)] == ["d/a.txt", "d/A~2.txt", "d/a~3.txt", "x", "x~2"]
