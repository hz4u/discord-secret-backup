import os

import pytest

from secret.core import textfile
from secret.core.textfile import ChangedOnDisk, NotText, TooLarge


def write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


@pytest.mark.parametrize("data, encoding, bom", [
    ("안녕\r\n세계".encode("utf-8"), "utf-8", False),
    (b"\xef\xbb\xbf" + "안녕\r\n세계".encode("utf-8"), "utf-8", True),
    ("안녕\r\n세계".encode("cp949"), "cp949", False),
    ("안녕\r\n세계".encode("utf-16"), "utf-16", True),
])
def test_detects_encoding(tmp_path, data, encoding, bom):
    doc = textfile.load(write(tmp_path, "a.txt", data))
    assert doc.text == "안녕\n세계"
    assert (doc.encoding, doc.bom, doc.newline) == (encoding, bom, "\r\n")


@pytest.mark.parametrize("raw, newline", [(b"a\nb", "\n"), (b"a\r\nb", "\r\n"), (b"a\rb", "\r"), (b"ab", "\r\n")])
def test_detects_newline(tmp_path, raw, newline):
    doc = textfile.load(write(tmp_path, "a.txt", raw))
    assert doc.newline == newline
    assert doc.text == ("a\nb" if raw != b"ab" else "ab")


@pytest.mark.parametrize("data", [
    "안녕\r\n세계".encode("utf-8"),
    b"\xef\xbb\xbf" + "첫 줄\n둘째 줄\n".encode("utf-8"),
    "메모\r\n".encode("cp949"),
    "unicode 메모\r\n".encode("utf-16"),
    b"line1\rline2",
])
def test_save_without_edits_is_byte_identical(tmp_path, data):
    p = write(tmp_path, "a.txt", data)
    doc = textfile.load(p)
    textfile.save(p, doc, doc.text)
    assert p.read_bytes() == data


def test_save_keeps_encoding_and_newlines(tmp_path):
    p = write(tmp_path, "a.txt", "하나\r\n둘".encode("cp949"))
    doc = textfile.load(p)
    new = textfile.save(p, doc, "하나\n둘\n셋")
    assert p.read_bytes() == "하나\r\n둘\r\n셋".encode("cp949")
    assert new.encoding == "cp949" and not new.switched_to_utf8


def test_cp949_switches_to_utf8_for_emoji(tmp_path):
    p = write(tmp_path, "a.txt", "메모".encode("cp949"))
    doc = textfile.load(p)
    new = textfile.save(p, doc, "메모 🙂")
    assert new.switched_to_utf8 and new.encoding == "utf-8"
    assert p.read_bytes() == "메모 🙂".encode("utf-8")
    assert textfile.load(p).text == "메모 🙂"


def test_binary_is_not_text(tmp_path):
    with pytest.raises(NotText):
        textfile.load(write(tmp_path, "a.txt", b"PK\x03\x04\x00\x00binary"))


def test_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(textfile, "MAX_EDIT_BYTES", 10)
    with pytest.raises(TooLarge):
        textfile.load(write(tmp_path, "a.txt", b"x" * 11))


def test_save_is_atomic_and_leaves_no_temp(tmp_path, monkeypatch):
    p = write(tmp_path, "a.txt", b"original")
    doc = textfile.load(p)

    def boom(*a):
        raise OSError("disk pulled")

    monkeypatch.setattr(textfile.os, "replace", boom)
    with pytest.raises(OSError):
        textfile.save(p, doc, "changed")
    assert p.read_bytes() == b"original"
    assert os.listdir(tmp_path) == ["a.txt"]


def test_changed_on_disk_is_detected(tmp_path):
    p = write(tmp_path, "a.txt", b"one")
    doc = textfile.load(p)
    p.write_bytes(b"someone else wrote this")
    with pytest.raises(ChangedOnDisk):
        textfile.save(p, doc, "mine")
    assert p.read_bytes() == b"someone else wrote this"
    textfile.save(p, doc, "mine", force=True)
    assert p.read_bytes() == b"mine"


def test_save_returns_doc_for_next_save(tmp_path):
    p = write(tmp_path, "a.txt", b"one")
    doc = textfile.save(p, textfile.load(p), "two")
    doc = textfile.save(p, doc, "three")
    assert p.read_bytes() == b"three"
