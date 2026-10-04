import hashlib
import os

import pytest

from secret.core import keys, scanner
from secret.core.config_store import ConfigStore, Settings
from secret.core.sync_engine import SyncEngine
from secret.core.timeline import apply_rollback, fetch, plan_rollback, restored_state
from tests.fake_discord import FakeDiscord

PW = "timeline passphrase"
PART = 1000


def touch(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def tree(root):
    s = scanner.scan(root)
    return {rel: (hashlib.sha256((root / rel).read_bytes()).hexdigest(), f.mtime_ns) for rel, f in s.files.items()}


def data_messages(fake) -> set[str]:
    control = fake.by_name("secret")["id"]
    index = next(c["id"] for c in fake.children(control))
    ids = set()
    for cid, msgs in fake.messages.items():
        ch = fake.channels_by_id.get(cid)
        if ch is None or cid == index or ch.get("parent_id") == index:
            continue
        ids |= set(msgs)
    return ids


def sync(root, fake, store):
    engine = SyncEngine(root, fake, store, part_size=PART)
    engine.load_manifest()
    result = engine.run(engine.plan())
    assert result.complete
    return engine


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "usb"
    touch(root, "사진/여행/바다.jpg", os.urandom(2500))
    touch(root, "메모/글.txt", b"first")
    touch(root, "영상/v.mp4", b"video")
    touch(root, "설치.tmp", b"not backed up")
    fake = FakeDiscord()
    store = ConfigStore.create(root / ".secret/config.dat", PW, Settings(dk=keys.new_data_key(), token="t", guild_id=fake.guild_id))
    engine = sync(root, fake, store)
    return root, fake, store, engine, tree(root)


def test_rollback_brings_the_vault_back_without_uploading(vault):
    root, fake, store, engine, v1_tree = vault
    touch(root, "메모/글.txt", b"second, edited")
    (root / "영상/v.mp4").unlink()
    touch(root, "새/추가.jpg", b"added later")
    engine = sync(root, fake, store)

    target = engine.state_at(1)
    plan = plan_rollback(target, scanner.scan(root))
    assert sorted(plan.fetch) == ["메모/글.txt", "영상/v.mp4"] and plan.remove == ["새/추가.jpg"]
    result = apply_rollback(fake, store.settings.dk, root, plan)
    assert not result.failed and not result.corrupt
    assert tree(root) == v1_tree
    assert (root / "설치.tmp").read_bytes() == b"not backed up"

    before = data_messages(fake)
    engine.commit_state(target, kind="rollback")
    assert data_messages(fake) <= before
    engine2 = SyncEngine(root, fake, store, part_size=PART)
    engine2.load_manifest()
    assert engine2.plan().is_empty


def test_rollback_is_retryable_after_a_cut(vault, monkeypatch):
    root, fake, store, engine, v1_tree = vault
    touch(root, "메모/글.txt", b"second, edited")
    touch(root, "사진/여행/바다.jpg", os.urandom(2600))
    engine = sync(root, fake, store)
    target = engine.state_at(1)
    real = fake.download
    calls = [0]

    def flaky(url):
        calls[0] += 1
        if calls[0] == 2:
            from secret.core.discord_api import NetworkError
            raise NetworkError("cable pulled")
        return real(url)

    monkeypatch.setattr(fake, "download", flaky)
    first = apply_rollback(fake, store.settings.dk, root, plan_rollback(target, scanner.scan(root)), workers=1)
    assert first.failed or first.cancelled
    assert not list(root.rglob("*.secret-part"))
    monkeypatch.setattr(fake, "download", real)
    again = plan_rollback(target, scanner.scan(root))
    assert apply_rollback(fake, store.settings.dk, root, again).failed == []
    assert tree(root) == v1_tree


def test_trash_restore_puts_the_file_back_and_reuses_its_channel(vault):
    root, fake, store, engine, v1_tree = vault
    (root / "영상/v.mp4").unlink()
    (root / "영상").rmdir()
    engine = sync(root, fake, store)
    item = engine.manifest.trash[0]
    assert item.rel == "영상/v.mp4" and item.entry.channel_id in engine.manifest.retired_channels

    got = fetch(fake, store.settings.dk, root, {item.rel: item.entry}, overwrite=False)
    assert got.saved_as == {"영상/v.mp4": "영상/v.mp4"} and (root / "영상/v.mp4").read_bytes() == b"video"
    state = restored_state(engine.manifest, {"영상/v.mp4": item.entry})
    assert state.channels["영상/"] == item.entry.channel_id
    engine.commit_state(state, kind="trash-restore", untrash={item.entry.message_id})
    assert engine.manifest.trash == [] and engine.manifest.retired_channels == {}
    engine2 = SyncEngine(root, fake, store, part_size=PART)
    engine2.load_manifest()
    assert engine2.plan().is_empty


def test_trash_restore_keeps_an_existing_file(vault):
    root, fake, store, engine, _ = vault
    old = engine.manifest.files["메모/글.txt"]
    (root / "메모/글.txt").unlink()
    engine = sync(root, fake, store)
    touch(root, "메모/글.txt", b"a new file with the same name")
    item = engine.manifest.trash[0]
    got = fetch(fake, store.settings.dk, root, {item.rel: item.entry}, overwrite=False)
    assert got.saved_as == {"메모/글.txt": "메모/글 (1).txt"}
    assert (root / "메모/글.txt").read_bytes() == b"a new file with the same name"
    assert (root / "메모/글 (1).txt").read_bytes() == b"first"
    assert item.entry.message_id == old.message_id
