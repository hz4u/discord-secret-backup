import os
import threading

from secret.core import duplicates
from secret.core.duplicates import find_duplicates
from secret.core.scanner import scan


def _write(root, rel, data, mtime=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, ns=(mtime, mtime))


def test_finds_same_content_and_keeps_the_oldest_first(tmp_path):
    _write(tmp_path, "사진/a.jpg", b"same picture", 3_000_000_000)
    _write(tmp_path, "백업/a (1).jpg", b"same picture", 1_000_000_000)
    _write(tmp_path, "다른/a.jpg", b"same pictur!", 2_000_000_000)
    _write(tmp_path, "c.txt", b"unique")
    _write(tmp_path, "빈1.txt", b"")
    _write(tmp_path, "빈2.txt", b"")
    r = find_duplicates(tmp_path, scan(tmp_path).files)
    assert [g.files for g in r.groups] == [["백업/a (1).jpg", "사진/a.jpg"]]
    assert r.wasted == len(b"same picture")


def test_big_files_that_differ_only_after_the_head(tmp_path, monkeypatch):
    monkeypatch.setattr(duplicates, "HEAD_BYTES", 8)
    monkeypatch.setattr(duplicates, "CHUNK", 5)
    head = b"H" * 8
    _write(tmp_path, "a.txt", head + b"tail-one")
    _write(tmp_path, "b.txt", head + b"tail-one")
    _write(tmp_path, "c.txt", head + b"tail-two")
    seen = []
    r = find_duplicates(tmp_path, scan(tmp_path).files, on_progress=lambda d, t, cur: seen.append((d, t)))
    assert [g.files for g in r.groups] == [["a.txt", "b.txt"]]
    assert seen and all(d <= t for d, t in seen)


def test_only_same_size_files_are_read(tmp_path, monkeypatch):
    _write(tmp_path, "a.txt", b"1")
    _write(tmp_path, "b.txt", b"22")
    opened = []
    real = duplicates._digest
    monkeypatch.setattr(duplicates, "_digest", lambda path, *a: (opened.append(path.name), real(path, *a))[1])
    assert find_duplicates(tmp_path, scan(tmp_path).files).groups == []
    assert opened == []


def test_cancel(tmp_path):
    _write(tmp_path, "a.txt", b"x" * 10)
    _write(tmp_path, "b.txt", b"x" * 10)
    stop = threading.Event()
    stop.set()
    assert find_duplicates(tmp_path, scan(tmp_path).files, cancel=stop).cancelled
