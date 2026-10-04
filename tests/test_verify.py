import os
import threading

import pytest

from secret.core import keys
from secret.core.config_store import ConfigStore, Settings
from secret.core.discord_api import NetworkError
from secret.core.restore import NoBackupError
from secret.core.sync_engine import SyncEngine
from secret.core.verify import verify_backup
from tests.fake_discord import FakeDiscord

PW = "central passphrase for secret"
PART = 1000


def touch(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def backup(tmp_path):
    root = tmp_path / "usb"
    touch(root, "사진/여행/바다.jpg", os.urandom(2500))
    touch(root, "사진/일상.png", os.urandom(300))
    touch(root, "문서/표.xlsx", b"excel")
    fake = FakeDiscord()
    store = ConfigStore.create(root / ".secret/config.dat", PW, Settings(dk=keys.new_data_key(), token="t", guild_id=fake.guild_id))
    engine = SyncEngine(root, fake, store, part_size=PART)
    assert engine.run(engine.plan()).complete
    return root, fake, store, engine.manifest


def check(fake, store, **kw):
    return verify_backup(fake, fake.guild_id, store.password, store.settings.dk, **kw)


def reasons(result):
    return {p.rel: p.reason for p in result.problems}


def blob_urls(fake, entry):
    if entry.thread_id is None:
        return [fake.messages[entry.channel_id][entry.message_id]["attachments"][0]["url"]]
    return [fake.messages[entry.thread_id][pid]["attachments"][0]["url"] for pid in entry.part_ids]


@pytest.mark.parametrize("deep", [False, True])
def test_healthy_backup(backup, deep):
    _, fake, store, _ = backup
    r = check(fake, store, deep=deep)
    assert (r.total, r.ok, r.problems) == (3, 3, [])
    assert r.latest_index_ok and r.password_opens_index and r.deep is deep
    assert r.healthy


def test_missing_part_message(backup):
    _, fake, store, m = backup
    e = m.files["사진/여행/바다.jpg"]
    del fake.messages[e.thread_id][e.part_ids[1]]
    r = check(fake, store)
    assert reasons(r) == {"사진/여행/바다.jpg": "조각 1/3개가 디스코드에 없습니다"}
    assert r.ok == 2 and not r.healthy


def test_deleted_channel(backup):
    _, fake, store, m = backup
    fake.delete_channel(m.files["문서/표.xlsx"].channel_id)
    r = check(fake, store)
    assert reasons(r) == {"문서/표.xlsx": "디스코드에 없습니다"}


def test_size_mismatch_found_by_quick_check(backup):
    _, fake, store, m = backup
    for msgs in fake.messages.values():
        for msg in msgs.values():
            for att in msg["attachments"]:
                if att["url"] == blob_urls(fake, m.files["사진/일상.png"])[0]:
                    att["size"] -= 1
    r = check(fake, store)
    assert reasons(r) == {"사진/일상.png": "디스코드에 있는 크기가 맞지 않습니다"}


def test_same_size_corruption_needs_deep_check(backup):
    _, fake, store, m = backup
    url = blob_urls(fake, m.files["사진/여행/바다.jpg"])[2]
    data = bytearray(fake.blobs[url])
    data[3] ^= 0xFF
    fake.blobs[url] = bytes(data)
    assert check(fake, store).healthy
    r = check(fake, store, deep=True)
    assert reasons(r) == {"사진/여행/바다.jpg": "내용이 손상됐습니다 (암호 조각 검증 실패)"}


def test_password_not_matching_discord_index(backup):
    root, fake, store, _ = backup
    store.change_password("a brand new passphrase")
    r = check(fake, store)
    assert r.ok == r.total and not r.password_opens_index
    assert not r.healthy
    assert SyncEngine(root, fake, store, part_size=PART).republish_index()
    assert check(fake, store).healthy


def test_broken_latest_index_uses_previous(backup):
    root, fake, store, _ = backup
    touch(root, "새파일.txt", b"new")
    engine = SyncEngine(root, fake, store, part_size=PART)
    engine.run(engine.plan())
    index = fake.by_name("index")["id"]
    newest = max(fake.messages[index].values(), key=lambda msg: int(msg["id"]))
    if newest["attachments"]:
        url = newest["attachments"][0]["url"]
    else:
        first = min(fake.messages[newest["thread"]["id"]].values(), key=lambda msg: int(msg["id"]))
        url = first["attachments"][0]["url"]
    fake.blobs[url] = b"garbage"
    r = check(fake, store)
    assert not r.latest_index_ok and r.previous_index_ok
    assert r.total == 3 and not r.healthy


def test_no_backup():
    with pytest.raises(NoBackupError):
        verify_backup(FakeDiscord(), "900", PW, keys.new_data_key())


def test_cancel(backup):
    _, fake, store, _ = backup
    cancel = threading.Event()
    real = fake.download
    count = [0]

    def download(url):
        count[0] += 1
        if count[0] == 2:
            cancel.set()
        return real(url)

    fake.download = download
    r = check(fake, store, deep=True, cancel=cancel, workers=1)
    assert r.cancelled and r.ok < r.total


def test_network_failure_aborts(backup):
    _, fake, store, _ = backup
    real = fake.download
    count = [0]

    def download(url):
        count[0] += 1
        if count[0] == 3:
            raise NetworkError("cable pulled")
        return real(url)

    fake.download = download
    r = check(fake, store, deep=True, workers=1)
    assert r.aborted and not r.healthy


def test_reupload_repairs_a_broken_file(backup):
    root, fake, store, m = backup
    e = m.files["사진/여행/바다.jpg"]
    del fake.messages[e.thread_id][e.part_ids[0]]
    assert not check(fake, store).healthy
    store.settings.reupload = ["사진/여행/바다.jpg"]
    store.save()
    engine = SyncEngine(root, fake, store, part_size=PART, keep_versions=0)
    plan = engine.plan()
    assert plan.changed == ["사진/여행/바다.jpg"] and not plan.new
    result = engine.run(plan)
    assert result.uploaded == ["사진/여행/바다.jpg"] and result.complete
    assert store.settings.reupload == []
    assert e.thread_id not in fake.channels_by_id
    assert check(fake, store, deep=True).healthy
