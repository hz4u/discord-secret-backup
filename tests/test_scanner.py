import os
import sys

import pytest

from secret.core import scanner
from secret.core.hidden import hide


def touch(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.mark.parametrize(
    "name, kind",
    [
        ("a.JPG", "image"), ("b.heic", "image"), ("c.mp4", "video"), ("d.MKV", "video"),
        ("e.pdf", "document"), ("f.xlsx", "document"), ("g.csv", "document"), ("h.txt", "document"),
        ("i.mp3", "music"), ("j.flac", "music"), ("k.zip", "other"), ("l.7z", "other"),
        ("m.exe", "other"), ("n.py", "other"), ("noext", "other"), (".hidden", "other"),
        ("o.hwp", "document"), ("p.DOCX", "document"), ("q.CR2", "image"), ("r.tif", "image"), ("s.m4v", "video"),
        ("t.ogg", "music"),
        ("u.tmp", None), ("v.crdownload", None), ("~$문서.docx", None), ("Thumbs.db", None),
        ("바다.jpg.secret-part", None),
    ],
)
def test_kind_of(name, kind):
    assert scanner.kind_of(name) == kind


@pytest.mark.parametrize(
    "rel, folder",
    [
        ("a.jpg", ("", "")),
        ("사진/a.jpg", ("사진", "")),
        ("사진/여행/a.jpg", ("사진", "여행")),
        ("사진/여행/2025/클립/a.jpg", ("사진", "여행")),
    ],
)
def test_logical_folder(rel, folder):
    assert scanner.logical_folder(rel) == folder


def test_channel_key():
    assert scanner.channel_key("사진", "여행") == "사진/여행"
    assert scanner.channel_key("", "") == "/"


def test_scan(tmp_path):
    touch(tmp_path, "루트.png", b"12345")
    touch(tmp_path, "사진/여행/바다.jpg")
    touch(tmp_path, "사진/여행/2025/클립/영상.mp4")
    touch(tmp_path, "사진/임시.tmp")
    (tmp_path / "빈폴더/속").mkdir(parents=True)
    touch(tmp_path, "Secret.exe")
    touch(tmp_path, ".secret/config.dat")
    touch(tmp_path, "System Volume Information/x.jpg")
    touch(tmp_path, "$RECYCLE.BIN/y.jpg")

    result = scanner.scan(tmp_path)
    assert set(result.files) == {"루트.png", "사진/여행/바다.jpg", "사진/여행/2025/클립/영상.mp4"}
    assert set(result.excluded) == {"사진/임시.tmp"}
    assert set(result.dirs) == {"사진", "사진/여행", "사진/여행/2025", "사진/여행/2025/클립", "빈폴더", "빈폴더/속"}
    f = result.files["루트.png"]
    assert f.size == 5 and f.kind == "image"
    assert f.mtime_ns == os.stat(tmp_path / "루트.png").st_mtime_ns
    assert result.excluded["사진/임시.tmp"].kind is None


def test_hidden_names_only_at_root(tmp_path):
    touch(tmp_path, "문서/.secret/note.txt")
    assert "문서/.secret/note.txt" in scanner.scan(tmp_path).files


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 파일 속성")
def test_hidden_or_system_entries_are_skipped(tmp_path):
    import ctypes

    touch(tmp_path, "사진/보임.jpg")
    decoy = tmp_path / "!@cSOEV"
    touch(tmp_path, "!@cSOEV/Photo.jpg")
    touch(tmp_path, "!@cSOEV/sub/Memo.txt")
    ctypes.windll.kernel32.SetFileAttributesW(str(decoy), 0x2 | 0x4)
    system_file = touch(tmp_path, "사진/desktop.ini")
    ctypes.windll.kernel32.SetFileAttributesW(str(system_file), 0x4)
    hidden_file = touch(tmp_path, "사진/숨김.png")
    hide(hidden_file)

    result = scanner.scan(tmp_path)
    assert set(result.files) == {"사진/보임.jpg"}
    assert result.excluded == {}
    assert result.dirs == ["사진"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 파일 속성")
def test_folders_hidden_by_secret_are_included_when_listed(tmp_path):
    from secret.core.hidden import set_folder_hidden

    touch(tmp_path, "사진/여행/a.jpg")
    touch(tmp_path, "비밀/b.jpg")
    set_folder_hidden(tmp_path / "비밀", True)
    assert set(scanner.scan(tmp_path).files) == {"사진/여행/a.jpg"}
    result = scanner.scan(tmp_path, include_hidden={"비밀"})
    assert set(result.files) == {"사진/여행/a.jpg", "비밀/b.jpg"}
    assert "비밀" in result.dirs


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 파일 속성")
def test_nested_hidden_folders_are_included_by_path(tmp_path):
    from secret.core.hidden import set_folder_hidden

    touch(tmp_path, "사진/여행/a.jpg")
    touch(tmp_path, "사진/일상/b.jpg")
    set_folder_hidden(tmp_path / "사진/여행", True)
    assert set(scanner.scan(tmp_path).files) == {"사진/일상/b.jpg"}
    assert set(scanner.scan(tmp_path, include_hidden={"사진/여행"}).files) == {"사진/여행/a.jpg", "사진/일상/b.jpg"}
    touch(tmp_path, "영상/여행/c.mp4")
    set_folder_hidden(tmp_path / "영상/여행", True)
    assert "영상/여행/c.mp4" not in scanner.scan(tmp_path, include_hidden={"사진/여행"}).files


def test_is_hidden_root_name():
    assert scanner.is_hidden_root_name("secret.EXE")
    assert scanner.is_hidden_root_name("System Volume Information")
    assert not scanner.is_hidden_root_name("사진")
