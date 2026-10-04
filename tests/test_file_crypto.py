import os

import pytest

from secret.core import file_crypto
from secret.core.crypto_core import DecryptionError, FormatError
from secret.core.file_crypto import (
    FileTooLargeError,
    decrypt_file,
    encrypt_file,
    is_encrypted_file,
    safe_name,
    unique_path,
)

PW = "file passphrase 1234"


def test_roundtrip_restores_name_and_bytes(tmp_path):
    src = tmp_path / "여권사진.jpg"
    data = os.urandom(50_000)
    src.write_bytes(data)

    enc = encrypt_file(src, PW)
    assert enc.parent == tmp_path
    assert enc.suffix == ".enc"
    assert "여권" not in enc.name
    assert is_encrypted_file(enc)
    assert data not in enc.read_bytes()

    src.unlink()
    out = decrypt_file(enc, PW)
    assert out == tmp_path / "여권사진.jpg"
    assert out.read_bytes() == data


def test_empty_file_roundtrip(tmp_path):
    src = tmp_path / "empty.txt"
    src.write_bytes(b"")
    enc = encrypt_file(src, PW)
    src.unlink()
    assert decrypt_file(enc, PW).read_bytes() == b""


def test_existing_name_not_overwritten(tmp_path):
    src = tmp_path / "photo.png"
    src.write_bytes(b"original")
    enc = encrypt_file(src, PW)

    first = decrypt_file(enc, PW)
    second = decrypt_file(enc, PW)
    assert first.name == "photo (1).png"
    assert second.name == "photo (2).png"
    assert src.read_bytes() == b"original"


def test_wrong_password(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    enc = encrypt_file(src, PW)
    with pytest.raises(DecryptionError):
        decrypt_file(enc, "nope")


def test_tampered_file(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"hello world")
    enc = encrypt_file(src, PW)
    raw = bytearray(enc.read_bytes())
    raw[-1] ^= 1
    enc.write_bytes(bytes(raw))
    with pytest.raises(DecryptionError):
        decrypt_file(enc, PW)


def test_not_encrypted_file_raises_format_error(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"just text")
    assert not is_encrypted_file(plain)
    with pytest.raises(FormatError):
        decrypt_file(plain, PW)


def test_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(file_crypto, "MAX_SIZE", 10)
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 11)
    with pytest.raises(FileTooLargeError):
        encrypt_file(src, PW)
    assert list(tmp_path.glob("*.enc")) == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("photo.jpg", "photo.jpg"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\x.dll", "x.dll"),
        ("..", "restored_file"),
        ("", "restored_file"),
        ("a:b*c?.txt", "a_b_c_.txt"),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


def test_malicious_name_inside_payload_stays_in_folder(tmp_path, monkeypatch):
    src = tmp_path / "x.bin"
    src.write_bytes(b"data")
    monkeypatch.setattr(file_crypto, "_original_name", lambda p: "..\\..\\evil.bin")
    enc = encrypt_file(src, PW)
    out = decrypt_file(enc, PW)
    assert out.parent == tmp_path
    assert out.name == "evil.bin"


def test_unique_path(tmp_path):
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a.txt"
    (tmp_path / "a.txt").write_text("1")
    assert unique_path(tmp_path, "a.txt") == tmp_path / "a (1).txt"
    (tmp_path / "noext").write_text("1")
    assert unique_path(tmp_path, "noext") == tmp_path / "noext (1)"


def test_is_encrypted_file_missing_or_short(tmp_path):
    assert not is_encrypted_file(tmp_path / "missing.enc")
    short = tmp_path / "s"
    short.write_bytes(b"FI")
    assert not is_encrypted_file(short)


def test_progress_goes_up_to_one(tmp_path, monkeypatch):
    monkeypatch.setattr("secret.core.file_crypto.IO_CHUNK", 1000)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(5500))
    seen = []
    enc = encrypt_file(src, PW, on_progress=seen.append)
    assert seen == sorted(seen) and seen[-1] == 1.0 and len(seen) > 6
    seen.clear()
    out = decrypt_file(enc, PW, on_progress=seen.append)
    assert seen == sorted(seen) and seen[-1] == 1.0
    assert out.read_bytes() == src.read_bytes()


def _multi_chunk(tmp_path, monkeypatch, size=5500):
    monkeypatch.setattr(file_crypto, "IO_CHUNK", 1000)
    src = tmp_path / "여러조각.bin"
    data = os.urandom(size)
    src.write_bytes(data)
    return src, data, encrypt_file(src, PW)


def test_file2_streams_in_chunks_and_roundtrips(tmp_path, monkeypatch):
    src, data, enc = _multi_chunk(tmp_path, monkeypatch)
    assert enc.read_bytes()[:5] == b"FILE2"
    assert enc.stat().st_size == file_crypto._HEADER2.size + len(data) + len("여러조각.bin".encode()) + 2 + 16 * 6
    src.unlink()
    out = decrypt_file(enc, PW)
    assert out.name == "여러조각.bin" and out.read_bytes() == data
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(file_crypto.TEMP_SUFFIX)] == []


def test_old_file1_files_still_open(tmp_path):
    from secret.core.crypto_core import encrypt_blob

    name = "예전.txt".encode()
    blob = b"FILE1" + encrypt_blob(PW, len(name).to_bytes(2, "big") + name + b"old data", b"FILE1")
    (tmp_path / "old.enc").write_bytes(blob)
    out = decrypt_file(tmp_path / "old.enc", PW)
    assert out.name == "예전.txt" and out.read_bytes() == b"old data"


@pytest.mark.parametrize("damage", ["truncate", "swap", "header", "append"])
def test_file2_detects_cut_swapped_or_edited_files(tmp_path, monkeypatch, damage):
    src, _, enc = _multi_chunk(tmp_path, monkeypatch)
    raw = bytearray(enc.read_bytes())
    h, full = file_crypto._HEADER2.size, 1000 + 16
    if damage == "truncate":
        raw = raw[: h + full * 2]
    elif damage == "swap":
        a, b = raw[h : h + full], raw[h + full : h + 2 * full]
        raw[h : h + full], raw[h + full : h + 2 * full] = b, a
    elif damage == "header":
        raw[10] ^= 1
    else:
        raw += b"extra"
    enc.write_bytes(bytes(raw))
    src.unlink()
    with pytest.raises((DecryptionError, FormatError)):
        decrypt_file(enc, PW)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == [enc.name]


def test_wrong_password_leaves_nothing_behind(tmp_path, monkeypatch):
    src, _, enc = _multi_chunk(tmp_path, monkeypatch)
    with pytest.raises(DecryptionError):
        decrypt_file(enc, "wrong")
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([src.name, enc.name])
