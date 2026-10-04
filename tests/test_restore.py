import hashlib
import os
import threading

import pytest

from secret.core import keys
from secret.core.config_store import ConfigStore, Settings
from secret.core.crypto_core import DecryptionError
from secret.core.discord_api import NetworkError
from secret.core.restore import PART_WINDOW, NoBackupError, restore, target_has_files
from secret.core.sync_engine import SyncEngine
from tests.fake_discord import FakeDiscord

PW = "central passphrase for secret"


def touch(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def tree(root):
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(".secret"):
            continue
        if p.is_file():
            out[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        else:
            out[rel] = "dir"
    return out


@pytest.fixture
def backed_up(tmp_path):
    root = tmp_path / "usb"
    touch(root, "사진/여행/바다.jpg", os.urandom(2500))
    touch(root, "사진/여행/2025/클립/깊은.mp4", os.urandom(3100))
    touch(root, "문서/표.xlsx", b"excel")
    touch(root, "루트.txt", b"")
    (root / "빈폴더/속").mkdir(parents=True)
    touch(root, "제외.tmp", b"no")
    fake = FakeDiscord()
    store = ConfigStore.create(root / ".secret/config.dat", PW, Settings(dk=keys.new_data_key(), token="t", guild_id=fake.guild_id))
    engine = SyncEngine(root, fake, store, part_size=1000)
    assert engine.run(engine.plan()).complete
    return root, fake


def test_full_restore_matches(backed_up, tmp_path):
    root, fake = backed_up
    target = tmp_path / "new_usb"
    target.mkdir()
    result = restore(fake, fake.guild_id, PW, target)
    assert result.verified == result.total == 4
    assert not result.mismatched and not result.failed
    expected = tree(root)
    del expected["제외.tmp"]
    assert tree(target) == expected


def test_wrong_password(backed_up, tmp_path):
    _, fake = backed_up
    with pytest.raises(DecryptionError):
        restore(fake, fake.guild_id, "wrong password", tmp_path / "x")


def test_no_backup(tmp_path):
    with pytest.raises(NoBackupError):
        restore(FakeDiscord(), "900", PW, tmp_path)


def test_existing_files_not_overwritten(backed_up, tmp_path):
    _, fake = backed_up
    target = tmp_path / "t"
    touch(target, "문서/표.xlsx", b"mine")
    result = restore(fake, fake.guild_id, PW, target)
    assert (target / "문서/표.xlsx").read_bytes() == b"mine"
    assert (target / "문서/표 (1).xlsx").read_bytes() == b"excel"
    assert result.renamed == [("문서/표.xlsx", "문서/표 (1).xlsx")]


def test_corrupt_latest_index_falls_back(backed_up, tmp_path):
    root, fake = backed_up
    touch(root, "새파일.txt", b"new")
    store = ConfigStore.open(root / ".secret/config.dat", PW)
    engine = SyncEngine(root, fake, store, part_size=1000)
    engine.run(engine.plan())
    index = fake.by_name("index")["id"]
    newest = max(fake.messages[index].values(), key=lambda m: int(m["id"]))
    if newest["attachments"]:
        url = newest["attachments"][0]["url"]
    else:
        first = min(fake.messages[newest["thread"]["id"]].values(), key=lambda m: int(m["id"]))
        url = first["attachments"][0]["url"]
    fake.blobs[url] = b"garbage"
    result = restore(fake, fake.guild_id, PW, tmp_path / "t")
    assert result.used_fallback_index
    assert result.total == 4


def test_tampered_part_is_reported(backed_up, tmp_path):
    _, fake = backed_up
    victim = next(url for url in fake.blobs if url.endswith(".002"))
    blob = bytearray(fake.blobs[victim])
    blob[5] ^= 1
    fake.blobs[victim] = bytes(blob)
    result = restore(fake, fake.guild_id, PW, tmp_path / "t")
    assert len(result.failed) == 1 and result.verified == 3


def test_password_change_then_republish(backed_up, tmp_path):
    root, fake = backed_up
    store = ConfigStore.open(root / ".secret/config.dat", PW)
    store.change_password("a brand new passphrase")
    assert SyncEngine(root, fake, store, part_size=1000).republish_index()
    result = restore(fake, fake.guild_id, "a brand new passphrase", tmp_path / "t")
    assert result.verified == 4
    index = fake.by_name("index")["id"]
    assert len(fake.messages[index]) == 2


def test_write_config_makes_usb_ready(backed_up, tmp_path):
    _, fake = backed_up
    target = tmp_path / "t"
    restore(fake, fake.guild_id, PW, target, write_config=True, token="tok")
    store = ConfigStore.open(target / ".secret/config.dat", PW)
    assert store.settings.token == "tok" and store.settings.guild_id == fake.guild_id
    engine = SyncEngine(target, fake, store, part_size=1000)
    engine.load_manifest()
    assert engine.plan().is_empty


def parts_left(root):
    return sorted(p.name for p in root.rglob("*.secret-part"))


def temp_files(root):
    return sorted(p.name for p in root.rglob("*") if p.name.endswith((".secret-part", ".part")))


def on_nth_download(fake, n, action):
    count = [0]

    def hook(url):
        count[0] += 1
        if count[0] == n:
            action()
    fake.on_download = hook
    return count


def cable_pulled():
    raise NetworkError("cable pulled")


def test_interrupted_restore_resumes_without_duplicates(backed_up, tmp_path):
    root, fake = backed_up
    target = tmp_path / "t"
    on_nth_download(fake, 5, cable_pulled)
    first = restore(fake, fake.guild_id, PW, target, workers=1, write_config=True, token="tok")
    assert first.cancelled and first.verified == 2
    assert temp_files(target) == []
    assert not (target / "사진/여행/2025/클립/깊은.mp4").exists()
    assert not (target / ".secret/config.dat").exists()

    fake.on_download = None
    second = restore(fake, fake.guild_id, PW, target, write_config=True, token="tok")
    assert not second.cancelled and not second.failed
    assert second.skipped == 2 and second.verified == 4 and second.renamed == []
    expected = tree(root)
    del expected["제외.tmp"]
    assert tree(target) == expected
    assert ConfigStore.exists(target / ".secret/config.dat")


def test_restore_again_skips_renamed_copy(backed_up, tmp_path):
    _, fake = backed_up
    target = tmp_path / "t"
    touch(target, "문서/표.xlsx", b"mine")
    restore(fake, fake.guild_id, PW, target)
    again = restore(fake, fake.guild_id, PW, target)
    assert again.skipped == 4 and again.renamed == []
    assert not (target / "문서/표 (2).xlsx").exists()
    assert (target / "문서/표.xlsx").read_bytes() == b"mine"


def test_cancel_stops_between_parts(backed_up, tmp_path):
    _, fake = backed_up
    target = tmp_path / "t"
    cancel = threading.Event()
    count = on_nth_download(fake, 4, cancel.set)
    result = restore(fake, fake.guild_id, PW, target, workers=1, cancel=cancel)
    assert result.cancelled
    assert count[0] <= 3 + PART_WINDOW < 3 + 4
    assert parts_left(target) == []
    assert not (target / "사진/여행/2025/클립/깊은.mp4").exists()


def test_stale_temp_files_are_removed(backed_up, tmp_path):
    _, fake = backed_up
    target = tmp_path / "t"
    touch(target, "사진/여행/바다.jpg.secret-part", b"half")
    touch(target, "사진/여행/브라우저.part", b"not ours")
    result = restore(fake, fake.guild_id, PW, target)
    assert result.verified == 4
    assert parts_left(target) == []
    assert (target / "사진/여행/브라우저.part").read_bytes() == b"not ours"


def test_target_check_ignores_drive_leftovers(tmp_path):
    from secret.core.hidden import hide

    drive = tmp_path / "hdd"
    drive.mkdir()
    assert not target_has_files(drive)
    (drive / "Secret.exe").write_bytes(b"exe")
    (drive / ".secret").mkdir()
    (drive / "System Volume Information").mkdir()
    (drive / "$RECYCLE.BIN").mkdir()
    decoy = drive / "!백신미끼"
    decoy.mkdir()
    hide(decoy)
    assert not target_has_files(drive)
    (drive / "사진").mkdir()
    assert target_has_files(drive)
    assert not target_has_files(tmp_path / "없는폴더")


def test_restore_asks_discord_once_per_channel_not_per_file(backed_up, tmp_path, monkeypatch):
    _, fake = backed_up
    calls = []
    real = fake.get_message
    monkeypatch.setattr(fake, "get_message", lambda *a: calls.append(a) or real(*a))
    result = restore(fake, fake.guild_id, PW, tmp_path / "t")
    assert result.verified == 4
    assert len(calls) <= 1


def test_expired_attachment_url_is_refreshed(backed_up, tmp_path, monkeypatch):
    _, fake = backed_up
    real_after = fake.messages_after

    def stale(channel_id, after="0", limit=100):
        return [{**m, "attachments": [{**m["attachments"][0], "url": "fake://expired"}]} if m.get("attachments") else m
                for m in real_after(channel_id, after, limit)]

    monkeypatch.setattr(fake, "messages_after", stale)
    result = restore(fake, fake.guild_id, PW, tmp_path / "t")
    assert result.verified == 4 and not result.failed


def test_memory_budget_is_shared_by_all_file_workers(backed_up, tmp_path, monkeypatch):
    import threading as _threading

    from secret.core import restore as restore_mod

    _, fake = backed_up
    monkeypatch.setattr(restore_mod, "MEMORY_BUDGET", 1016)
    lock = _threading.Lock()
    in_flight, peak = [0], [0]
    real = fake.download

    def counting(url):
        size = len(fake.blobs[url])
        with lock:
            in_flight[0] += size
            peak[0] = max(peak[0], in_flight[0])
        try:
            _threading.Event().wait(0.01)
            return real(url)
        finally:
            with lock:
                in_flight[0] -= size

    result = None
    fake.download = counting
    result = restore(fake, fake.guild_id, PW, tmp_path / "t", workers=6)
    assert result.verified == 4
    assert peak[0] <= 1016


def test_part_bigger_than_the_budget_still_downloads(backed_up, tmp_path, monkeypatch):
    from secret.core import restore as restore_mod

    _, fake = backed_up
    monkeypatch.setattr(restore_mod, "MEMORY_BUDGET", 10)
    result = restore(fake, fake.guild_id, PW, tmp_path / "t", workers=6)
    assert result.verified == 4 and not result.failed

def test_url_book_reads_long_channels_in_pages(monkeypatch):
    from secret.core.restore import _UrlBook

    fake = FakeDiscord()
    cid = fake.create_text_channel(fake.guild_id, "c", fake.create_category(fake.guild_id, "v"))
    ids = [fake.send_file(cid, "", f"{i}.bin", b"x")["id"] for i in range(250)]
    pages = []
    real = fake.messages_after
    monkeypatch.setattr(fake, "messages_after", lambda *a: pages.append(a) or real(*a))
    monkeypatch.setattr(fake, "get_message", lambda *a: pytest.fail("목록에 있으면 따로 묻지 않는다"))
    book = _UrlBook(fake)
    assert book.url(cid, ids[0]) and book.url(cid, ids[-1]) and book.url(cid, ids[137])
    assert len(pages) == 3
