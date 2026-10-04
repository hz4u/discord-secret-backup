import sys
import threading
import time

import pytest

from secret.core.fswatch import FolderWatcher, _parse, is_relevant

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 폴더 알림")


def test_relevance():
    assert is_relevant("사진\\여행\\a.jpg") and is_relevant("새 폴더")
    assert not is_relevant(".secret\\config.dat") and not is_relevant(".SECRET\\config.dat.tmp")
    assert not is_relevant("사진\\a.jpg.secret-part") and not is_relevant("메모.txt.secret-tmp".replace("메모", ".메모"))
    assert not is_relevant("$RECYCLE.BIN\\x") and not is_relevant("")


def test_parse_file_notify_information():
    def entry(name, last=False):
        raw = name.encode("utf-16-le")
        size = 12 + len(raw)
        pad = (-size) % 4
        nxt = 0 if last else size + pad
        return nxt.to_bytes(4, "little") + (1).to_bytes(4, "little") + len(raw).to_bytes(4, "little") + raw + b"\0" * pad

    assert _parse(entry("사진\\a.jpg") + entry("b", last=True)) == ["사진\\a.jpg", "b"]


class Recorder:
    def __init__(self):
        self.hits = 0
        self.event = threading.Event()

    def __call__(self):
        self.hits += 1
        self.event.set()

    def wait(self, timeout=3.0) -> bool:
        ok = self.event.wait(timeout)
        self.event.clear()
        return ok


@pytest.fixture
def watched(tmp_path):
    (tmp_path / "사진").mkdir()
    rec = Recorder()
    w = FolderWatcher(tmp_path, rec)
    assert w.start()
    time.sleep(0.2)
    yield tmp_path, rec, w
    w.stop()
    assert not w.running


def test_add_rename_delete_in_subfolders_are_reported(watched):
    root, rec, _ = watched
    (root / "사진" / "a.jpg").write_bytes(b"a")
    assert rec.wait()
    (root / "사진").rename(root / "그림")
    assert rec.wait()
    (root / "그림" / "a.jpg").unlink()
    assert rec.wait()


def test_secret_folder_and_temp_files_are_ignored(watched):
    root, rec, _ = watched
    (root / ".secret").mkdir()
    (root / ".secret" / "config.dat").write_bytes(b"x" * 100)
    (root / "사진" / "a.jpg.secret-part").write_bytes(b"half")
    assert not rec.wait(0.8)


def test_stop_is_quick(tmp_path):
    w = FolderWatcher(tmp_path, lambda: None)
    assert w.start()
    start = time.monotonic()
    w.stop()
    assert time.monotonic() - start < 1.5 and not w.running


def test_missing_folder_does_not_start(tmp_path):
    assert not FolderWatcher(tmp_path / "없음", lambda: None).start()
