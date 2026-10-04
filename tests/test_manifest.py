import dataclasses
import json
import os

import pytest

from secret.core import keys, manifest
from secret.core.crypto_core import DecryptionError
from secret.core.manifest import FileEntry, Manifest, ManifestError

PW = "central passphrase for secret"


def sample() -> Manifest:
    m = Manifest.empty()
    m.categories["사진"] = "100"
    m.channels["사진/여행"] = "200"
    m.files["사진/여행/바다.jpg"] = FileEntry(
        size=10, mtime_ns=123, sha256="ab" * 32, file_id="00" * 16, nonce_prefix="11" * 8, n=1,
        channel_id="200", message_id="300", thread_id=None, part_ids=["300"],
    )
    m.dirs = ["사진", "사진/여행"]
    return m


def test_json_roundtrip():
    m = sample()
    again = Manifest.from_json(m.to_json())
    assert again == m


def test_index_roundtrip():
    dk = keys.new_data_key()
    header = keys.wrap(dk, PW)
    blob = manifest.encode_index(sample(), dk, header)
    assert blob.startswith(manifest.INDEX_MAGIC)
    assert "바다".encode() not in blob
    read_header = manifest.read_index_header(blob)
    assert keys.unwrap(read_header, PW) == dk
    assert manifest.decode_index(blob, dk) == sample()


def test_large_index_spans_many_chunks(monkeypatch):
    monkeypatch.setattr(manifest, "INNER_CHUNK", 64)
    dk = keys.new_data_key()
    m = Manifest.empty()
    for _ in range(200):
        m.dirs.append(os.urandom(8).hex())
    blob = manifest.encode_index(m, dk, keys.wrap(dk, PW))
    assert manifest.decode_index(blob, dk) == m


def test_wrong_dk():
    dk = keys.new_data_key()
    blob = manifest.encode_index(sample(), dk, keys.wrap(dk, PW))
    with pytest.raises(DecryptionError):
        manifest.decode_index(blob, keys.new_data_key())


@pytest.mark.parametrize("corrupt", [lambda b: b"NOPE" + b[4:], lambda b: b[:10], lambda b: b[:-5]])
def test_corrupt_blob(corrupt):
    dk = keys.new_data_key()
    blob = manifest.encode_index(sample(), dk, keys.wrap(dk, PW))
    with pytest.raises((ManifestError, DecryptionError)):
        manifest.decode_index(corrupt(blob), dk)


def test_empty_manifest():
    m = Manifest.empty()
    assert m.files == {} and m.categories == {} and m.channels == {} and m.dirs == []


def _fe(i: int) -> manifest.FileEntry:
    return manifest.FileEntry(size=i, mtime_ns=i, sha256=f"{i:064x}", file_id="00" * 16, nonce_prefix="00" * 8,
                              n=1, channel_id="c1", message_id=f"m{i}", thread_id=None, part_ids=[f"m{i}"])


def test_history_and_trash_roundtrip():
    m = Manifest.empty()
    m.files["a.jpg"] = _fe(1)
    m.history = [manifest.Version(id=1, at="2026-09-30T10:00:00", kind="sync", summary={"new": 1},
                                  changes={"a.jpg": (None, _fe(1)), "old.jpg": (_fe(9), None)},
                                  dirs=["사진"], channels={"사진/": "c1"}, categories={"사진": "k1"})]
    m.trash = [manifest.TrashItem(rel="old.jpg", entry=_fe(9), at="2026-09-30T10:00:00", batch=1)]
    m.retired_channels = {"c9": "k9"}
    m.retired_categories = ["k9"]
    back = Manifest.from_json(json.loads(json.dumps(m.to_json())))
    assert back == m


def test_old_format_without_history_still_reads():
    old = {"v": 1, "updated": "", "categories": {}, "channels": {}, "files": {}, "dirs": []}
    m = Manifest.from_json(old)
    assert m.history == [] and m.trash == [] and m.retired_channels == {} and m.retired_categories == []


def test_new_fields_do_not_change_what_old_readers_see():
    m = Manifest.empty()
    m.files["a.jpg"] = _fe(1)
    m.history = [manifest.Version(id=1, at="t", kind="sync")]
    data = m.to_json()
    assert data["v"] == 1
    assert {k: data[k] for k in ("categories", "channels", "files", "dirs")} == {
        "categories": {}, "channels": {}, "files": {"a.jpg": dataclasses.asdict(_fe(1))}, "dirs": []}
