import os

import pytest

from secret.core import chunks
from secret.core.crypto_core import DecryptionError

KEY = bytes(range(32))
FID = bytes(16)
PREFIX = bytes([7]) * 8


def test_limits():
    MiB = 1024 * 1024
    assert chunks.upload_limit_for_tier(0) == 10 * MiB
    assert chunks.upload_limit_for_tier(1) == 10 * MiB
    assert chunks.upload_limit_for_tier(2) == 50 * MiB
    assert chunks.upload_limit_for_tier(3) == 100 * MiB
    assert chunks.part_size_for_limit(10 * MiB) == 10 * MiB - 64 * 1024


@pytest.mark.parametrize("size, part, n", [(0, 10, 1), (1, 10, 1), (10, 10, 1), (11, 10, 2), (95, 10, 10)])
def test_part_count(size, part, n):
    assert chunks.part_count(size, part) == n


def test_roundtrip():
    ct = chunks.encrypt_part(KEY, FID, PREFIX, 3, 5, b"hello")
    assert len(ct) == 5 + 16
    assert chunks.decrypt_part(KEY, FID, PREFIX, 3, 5, ct) == b"hello"


@pytest.mark.parametrize(
    "change",
    [
        {"index": 2},
        {"total": 4},
        {"file_id": bytes([1]) * 16},
        {"key": bytes(32)},
        {"prefix": bytes(8)},
    ],
)
def test_context_is_authenticated(change):
    ct = chunks.encrypt_part(KEY, FID, PREFIX, 3, 5, b"hello")
    args = {"key": KEY, "file_id": FID, "prefix": PREFIX, "index": 3, "total": 5} | change
    with pytest.raises(DecryptionError):
        chunks.decrypt_part(args["key"], args["file_id"], args["prefix"], args["index"], args["total"], ct)


def test_tamper():
    ct = bytearray(chunks.encrypt_part(KEY, FID, PREFIX, 0, 1, b"hello"))
    ct[0] ^= 1
    with pytest.raises(DecryptionError):
        chunks.decrypt_part(KEY, FID, PREFIX, 0, 1, bytes(ct))


def test_read_part(tmp_path):
    data = os.urandom(25)
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    assert [chunks.read_part(f, i, 10) for i in range(3)] == [data[:10], data[10:20], data[20:]]


def test_empty_file_single_part(tmp_path):
    f = tmp_path / "empty"
    f.write_bytes(b"")
    assert chunks.part_count(0, 10) == 1
    assert chunks.read_part(f, 0, 10) == b""
    ct = chunks.encrypt_part(KEY, FID, PREFIX, 0, 1, b"")
    assert chunks.decrypt_part(KEY, FID, PREFIX, 0, 1, ct) == b""


def test_random_ids():
    assert len(chunks.new_file_id()) == 16
    assert len(chunks.new_nonce_prefix()) == 8
    assert chunks.new_file_id() != chunks.new_file_id()
