import os
import sys
import threading

import pytest

from secret.core import keys
from secret.core import manifest as manifest_mod
from secret.core import restore as restore_mod
from secret.core.config_store import ConfigStore, Settings
from secret.core.discord_api import DiscordError, NetworkError
from secret.core.restore import restore
from secret.core.sync_engine import SyncEngine
from tests.fake_discord import BOT_ID, FakeDiscord

PW = "central passphrase for secret"
PART = 1000


def touch(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "usb"
    root.mkdir()
    fake = FakeDiscord()
    store = ConfigStore.create(
        root / ".secret" / "config.dat", PW, Settings(dk=keys.new_data_key(), token="t", guild_id=fake.guild_id)
    )
    return root, fake, store


def sync(root, fake, store, **kw):
    kw.setdefault("keep_versions", 0)
    kw.setdefault("keep_trash", 0)
    engine = SyncEngine(root, fake, store, part_size=kw.pop("part_size", PART), **kw)
    plan = engine.plan()
    return engine, plan, engine.run(plan)


def data_bot_messages(fake):
    control = fake.by_name("secret")["id"]
    index = next(c["id"] for c in fake.children(control))
    ids = set()
    for cid, msgs in fake.messages.items():
        if cid == index or fake.channels_by_id[cid].get("parent_id") == index:
            continue
        ids |= {m["id"] for m in msgs.values() if m["author"]["id"] == BOT_ID}
    return ids


def referenced(m):
    ids = set()
    for e in m.files.values():
        ids |= {e.message_id, *e.part_ids}
    return ids


def test_first_backup_structure(env):
    root, fake, store = env
    touch(root, "사진/여행/바다.jpg", b"sea" * 10)
    touch(root, "사진/일상.png", b"daily")
    touch(root, "메모.txt", b"memo")
    touch(root, "무시.tmp", b"exe")
    _, plan, result = sync(root, fake, store)
    assert result.complete
    assert sorted(result.uploaded) == ["메모.txt", "사진/여행/바다.jpg", "사진/일상.png"]

    names = [c["name"] for c in fake.channels_by_id.values()]
    assert "secret" in names and "index" in names
    for n in names:
        assert n in ("secret", "index") or n[:2] in ("v-", "c-", "t-"), n
    everything = " ".join(names) + " ".join(m["content"] for m in fake.all_messages())
    assert "사진" not in everything and "바다" not in everything

    m = manifest_mod.decode_index(fake.blobs[next(iter(
        m["attachments"][0]["url"] for m in fake.messages[fake.by_name("index")["id"]].values()
    ))], store.settings.dk)
    assert set(m.files) == {"메모.txt", "사진/여행/바다.jpg", "사진/일상.png"}
    assert m.channels.keys() == {"/", "사진/", "사진/여행"}
    assert store.settings.last_sync and store.settings.journal == {}
    assert store.settings.history[0]["uploaded"] == 3


def test_big_file_uses_thread(env):
    root, fake, store = env
    touch(root, "영상/게임/클립.mp4", os.urandom(PART * 2 + 10))
    engine, _, result = sync(root, fake, store)
    e = engine.manifest.files["영상/게임/클립.mp4"]
    assert e.n == 3 and e.thread_id and len(e.part_ids) == 3
    thread_msgs = fake.messages[e.thread_id]
    assert sorted(a["filename"][-4:] for m in thread_msgs.values() for a in m["attachments"]) == [".001", ".002", ".003"]


def test_second_sync_changes_only(env):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"aaa")
    touch(root, "사진/b.jpg", b"bbb")
    engine, _, _ = sync(root, fake, store)
    old_a, old_b = engine.manifest.files["사진/a.jpg"], engine.manifest.files["사진/b.jpg"]

    touch(root, "사진/a.jpg", b"changed content")
    engine2, plan, result = sync(root, fake, store)
    assert plan.changed == ["사진/a.jpg"] and plan.new == []
    assert result.uploaded == ["사진/a.jpg"]
    assert engine2.manifest.files["사진/b.jpg"].message_id == old_b.message_id
    ch = fake.messages[old_a.channel_id]
    assert old_a.message_id not in ch
    assert data_bot_messages(fake) == referenced(engine2.manifest)


def test_manifest_cache_avoids_download(env, monkeypatch):
    root, fake, store = env
    touch(root, "a.txt")
    sync(root, fake, store)
    calls = []
    monkeypatch.setattr(fake, "download", lambda url: calls.append(url) or b"")
    engine = SyncEngine(root, fake, store, part_size=PART)
    engine.load_manifest()
    assert calls == [] and "a.txt" in engine.manifest.files


@pytest.mark.parametrize("remembered", [True, False])
def test_a_second_secret_category_does_not_hide_the_backup(env, remembered):
    from secret.core.discord_api import TYPE_CATEGORY, TYPE_TEXT

    root, fake, store = env
    touch(root, "사진/a.jpg", b"a" * 30)
    touch(root, "사진/b.jpg", b"b" * 30)
    sync(root, fake, store)
    if not remembered:
        store.settings.index_channel_id = None
        store.save()
    decoy_cat = fake._create("secret", TYPE_CATEGORY)
    decoy_index = fake._create("index", TYPE_TEXT, decoy_cat)
    fake.messages[decoy_index] = {"9": {"id": "9", "content": "secret-index 가짜", "author": {"id": "someone"}, "attachments": []}}
    first = {decoy_cat: fake.channels_by_id.pop(decoy_cat), decoy_index: fake.channels_by_id.pop(decoy_index)}
    fake.channels_by_id = {**first, **fake.channels_by_id}

    touch(root, "사진/c.jpg", b"c" * 30)
    engine, plan, result = sync(root, fake, store)
    assert plan.new == ["사진/c.jpg"] and result.uploaded == ["사진/c.jpg"]
    assert engine.index_channel != decoy_index and store.settings.index_channel_id == engine.index_channel
    m, *_ = restore_mod.open_backup(fake, fake.guild_id, PW)
    assert set(m.files) == {"사진/a.jpg", "사진/b.jpg", "사진/c.jpg"}


def test_half_uploaded_index_is_skipped_and_cleaned_up(env):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"a" * 30)
    engine, _, _ = sync(root, fake, store)
    index = engine.index_channel
    head = fake.send_message(index, "secret-index v1 parts=3")
    tid = fake.create_thread(index, head["id"], "t-broken")
    fake.send_file(tid, "", "index.001", b"x" * 100)

    touch(root, "사진/b.jpg", b"b" * 30)
    engine, plan, result = sync(root, fake, store)
    assert result.complete and plan.new == ["사진/b.jpg"]
    assert head["id"] not in fake.messages[index] and tid not in fake.channels_by_id
    m, *_ = restore_mod.open_backup(fake, fake.guild_id, PW)
    assert set(m.files) == {"사진/a.jpg", "사진/b.jpg"}


def test_unreadable_folder_keeps_its_backup(env, monkeypatch):
    from secret.core import scanner

    root, fake, store = env
    touch(root, "TOP secret/xxxx/a.jpg", b"a" * 20)
    touch(root, "일반/b.jpg", b"b" * 20)
    sync(root, fake, store)
    before = store.settings.manifest
    touch(root, "일반/새.jpg", b"new")
    real = scanner.scan

    def broken(path, include_hidden=()):
        r = real(path, include_hidden)
        r.files.pop("TOP secret/xxxx/a.jpg")
        r.unreadable.append("TOP secret/xxxx")
        return r

    monkeypatch.setattr(scanner, "scan", broken)
    engine, plan, result = sync(root, fake, store)
    assert result.complete and plan.deleted == [] and plan.unseen == ["TOP secret/xxxx/a.jpg"]
    m = engine.manifest
    assert "TOP secret/xxxx/a.jpg" in m.files and "일반/새.jpg" in m.files
    assert referenced(m) <= data_bot_messages(fake)
    assert before != store.settings.manifest


def test_delete_file_channel_category(env):
    root, fake, store = env
    touch(root, "영상/게임/a.mp4", b"video")
    touch(root, "사진/b.jpg", b"pic")
    engine, _, _ = sync(root, fake, store)
    game_channel = engine.manifest.channels["영상/게임"]
    video_cat = engine.manifest.categories["영상"]
    (root / "영상/게임/a.mp4").unlink()
    (root / "영상/게임").rmdir()
    (root / "영상").rmdir()
    engine2, plan, result = sync(root, fake, store)
    assert result.deleted == ["영상/게임/a.mp4"]
    assert game_channel not in fake.channels_by_id and video_cat not in fake.channels_by_id
    assert set(engine2.manifest.files) == {"사진/b.jpg"}


def test_rename_uploads_nothing(env):
    root, fake, store = env
    touch(root, "사진/여행/a.jpg", b"photo")
    sync(root, fake, store)
    before = fake.uploads
    (root / "사진/여행/a.jpg").rename(root / "사진/여행/b.jpg")
    engine, plan, result = sync(root, fake, store)
    assert plan.renamed == [("사진/여행/a.jpg", "사진/여행/b.jpg")]
    assert fake.uploads == before + 1
    assert "사진/여행/b.jpg" in engine.manifest.files


def test_resume_after_network_failure(env):
    root, fake, store = env
    for i in range(4):
        touch(root, f"사진/{i}.jpg", os.urandom(PART * 2 + 5))
    fake.fail_after_uploads = 5
    _, _, result = sync(root, fake, store, workers=1)
    assert result.aborted and not result.complete
    assert store.settings.journal["done"] or store.settings.journal["files"]
    uploaded_before = fake.uploads

    fake.fail_after_uploads = None
    engine, _, result = sync(root, fake, store, workers=1)
    assert result.complete
    assert fake.uploads - uploaded_before == 12 - uploaded_before + 1
    assert data_bot_messages(fake) == referenced(engine.manifest)
    assert store.settings.journal == {}


def test_cancel_then_resume(env):
    root, fake, store = env
    for i in range(3):
        touch(root, f"f{i}.txt", os.urandom(PART * 3))
    cancel = threading.Event()

    def on_progress(p):
        if p.done_files >= 1:
            cancel.set()

    _, _, result = sync(root, fake, store, workers=1, cancel=cancel, on_progress=on_progress)
    assert result.cancelled
    engine, _, result = sync(root, fake, store, workers=1)
    assert result.complete and len(engine.manifest.files) == 3
    assert data_bot_messages(fake) == referenced(engine.manifest)


def test_human_messages_are_never_deleted(env):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"a")
    engine, _, _ = sync(root, fake, store)
    channel = engine.manifest.channels["사진/"]
    human = fake.post_as_user(channel, "사람이 쓴 메모")
    touch(root, "사진/a.jpg", b"changed!!")
    sync(root, fake, store)
    assert human in fake.messages[channel]


def test_channel_with_human_messages_is_kept(env):
    root, fake, store = env
    touch(root, "사진/여행/a.jpg", b"a")
    touch(root, "사진/일상/b.jpg", b"b")
    engine, _, _ = sync(root, fake, store)
    travel = engine.manifest.channels["사진/여행"]
    file_msg = engine.manifest.files["사진/여행/a.jpg"].message_id
    memo = fake.post_as_user(travel, "여행 폴더 메모")

    (root / "사진/여행/a.jpg").unlink()
    (root / "사진/여행").rmdir()
    engine, plan, result = sync(root, fake, store)
    assert plan.delete_channels == ["사진/여행"]
    assert travel in fake.channels_by_id
    assert memo in fake.messages[travel]
    assert file_msg not in fake.messages[travel]
    assert result.kept_channels == ["사진/여행"]
    assert "사진/여행" not in engine.manifest.channels


def test_category_kept_while_it_holds_a_kept_channel(env):
    root, fake, store = env
    touch(root, "영상/게임/a.mp4", b"a")
    engine, _, _ = sync(root, fake, store)
    category = engine.manifest.categories["영상"]
    channel = engine.manifest.channels["영상/게임"]
    fake.post_as_user(channel, "메모")
    (root / "영상/게임/a.mp4").unlink()
    (root / "영상/게임").rmdir()
    (root / "영상").rmdir()
    engine, _, result = sync(root, fake, store)
    assert category in fake.channels_by_id and channel in fake.channels_by_id
    assert fake.channels_by_id[channel]["parent_id"] == category
    assert "영상" not in engine.manifest.categories


def test_thread_with_human_reply_is_kept(env):
    root, fake, store = env
    touch(root, "a.zip", os.urandom(PART * 2))
    engine, _, _ = sync(root, fake, store)
    thread = engine.manifest.files["a.zip"].thread_id
    reply = fake.post_as_user(thread, "이 압축 파일 비밀번호 힌트")
    (root / "a.zip").unlink()
    _, _, result = sync(root, fake, store)
    assert result.deleted == ["a.zip"]
    assert thread in fake.channels_by_id and reply in fake.messages[thread]


def test_failed_upload_skips_deletions(env, monkeypatch):
    root, fake, store = env
    touch(root, "사진/여행/a.jpg", b"photo")
    touch(root, "사진/여행/keep.jpg", b"keep")
    touch(root, "사진/일상/b.jpg", b"b")
    engine, _, _ = sync(root, fake, store)
    old = engine.manifest.files["사진/여행/a.jpg"]
    (root / "사진/여행/a.jpg").rename(root / "사진/일상/a.jpg")

    real = fake.send_file

    def flaky(channel_id, content, filename, data):
        if filename != "index.bin":
            raise DiscordError("413: too large")
        return real(channel_id, content, filename, data)

    monkeypatch.setattr(fake, "send_file", flaky)
    _, plan, result = sync(root, fake, store)
    assert plan.deleted == ["사진/여행/a.jpg"]
    assert result.failed and result.deleted == []
    assert old.message_id in fake.messages[old.channel_id]


def test_index_keeps_latest_two(env):
    root, fake, store = env
    for i in range(4):
        touch(root, f"f{i}.txt", str(i).encode())
        sync(root, fake, store)
    index = fake.by_name("index")["id"]
    assert len(fake.messages[index]) == 2


def test_events_are_reported(env):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"a")
    touch(root, "사진/b.jpg", b"b")
    events = []
    engine = SyncEngine(root, fake, store, part_size=PART, on_event=lambda text, level: events.append((level, text)),
                        keep_versions=0, keep_trash=0)
    engine.run(engine.plan())
    texts = [t for _, t in events]
    assert "올림: 사진/a.jpg" in texts and "올림: 사진/b.jpg" in texts
    assert any(t.startswith("디스코드에 카테고리") for t in texts)
    assert texts[-1].startswith("목차를 저장했습니다")
    (root / "사진/a.jpg").unlink()
    events.clear()
    engine = SyncEngine(root, fake, store, part_size=PART, on_event=lambda text, level: events.append((level, text)),
                        keep_versions=0, keep_trash=0)
    engine.run(engine.plan())
    assert ("info", "삭제됨: 사진/a.jpg") in events


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 파일 속성")
def test_hidden_folders_are_backed_up_and_never_seen_as_deleted(env):
    from secret.core.hidden import set_folder_hidden

    root, fake, store = env
    touch(root, "비밀/a.jpg", b"secret photo")
    touch(root, "사진/b.jpg", b"b")
    engine, _, _ = sync(root, fake, store)
    assert "비밀/a.jpg" in engine.manifest.files

    set_folder_hidden(root / "비밀", True)
    store.settings.hidden_folders = ["비밀"]
    store.save()
    engine, plan, result = sync(root, fake, store)
    assert plan.deleted == [] and plan.is_empty
    touch(root, "비밀/c.jpg", b"new")
    engine, plan, result = sync(root, fake, store)
    assert plan.new == ["비밀/c.jpg"]


def test_file_changed_during_upload_is_deferred(env):
    root, fake, store = env
    path = touch(root, "a.txt", os.urandom(PART * 2))
    real = fake.send_file
    state = {"done": False}

    def sneaky(channel_id, content, filename, data):
        msg = real(channel_id, content, filename, data)
        if not state["done"] and filename != "index.bin":
            state["done"] = True
            path.write_bytes(os.urandom(PART * 2 + 1))
        return msg

    fake.send_file = sneaky
    engine, _, result = sync(root, fake, store)
    assert result.deferred == ["a.txt"] and "a.txt" not in engine.manifest.files
    assert data_bot_messages(fake) == set()
    fake.send_file = real
    engine, plan, result = sync(root, fake, store)
    assert result.uploaded == ["a.txt"]


def test_cut_off_while_deleting_keeps_latest_index_restorable(env, monkeypatch, tmp_path):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"old a")
    touch(root, "사진/b.jpg", b"bbb")
    touch(root, "영상/c.mp4", b"video")
    engine, _, _ = sync(root, fake, store)
    old_a = engine.manifest.files["사진/a.jpg"]
    touch(root, "사진/a.jpg", b"new content for a")
    (root / "영상/c.mp4").unlink()
    (root / "영상").rmdir()

    index = fake.by_name("index")["id"]
    real_delete = fake.delete_message
    allowed = [1]

    def flaky_delete(channel_id, message_id):
        if channel_id != index:
            if not allowed[0]:
                raise NetworkError("cable pulled")
            allowed[0] -= 1
        return real_delete(channel_id, message_id)

    monkeypatch.setattr(fake, "delete_message", flaky_delete)
    _, _, result = sync(root, fake, store)
    assert result.aborted
    assert store.settings.journal["to_delete"]

    r = restore(fake, fake.guild_id, PW, tmp_path / "restored")
    assert not r.used_fallback_index and not r.failed and r.verified == r.total == 2
    assert (tmp_path / "restored/사진/a.jpg").read_bytes() == b"new content for a"

    monkeypatch.setattr(fake, "delete_message", real_delete)
    engine, plan, result = sync(root, fake, store)
    assert plan.is_empty and result.complete
    assert old_a.message_id not in fake.messages[old_a.channel_id]
    assert set(engine.manifest.files) == {"사진/a.jpg", "사진/b.jpg"}
    assert data_bot_messages(fake) == referenced(engine.manifest)
    assert store.settings.journal == {}


def test_leftover_delete_never_touches_what_the_index_uses(env):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"aaa")
    engine, _, _ = sync(root, fake, store)
    live = engine.manifest.files["사진/a.jpg"]
    store.settings.journal = {"to_delete": [
        {"kind": "file", "rel": "사진/a.jpg", "entry": vars(live).copy()},
        {"kind": "channel", "key": "사진", "id": live.channel_id},
    ]}
    touch(root, "사진/b.jpg", b"bbb")
    engine, _, result = sync(root, fake, store)
    assert result.complete and result.deleted == []
    assert set(engine.manifest.files) == {"사진/a.jpg", "사진/b.jpg"}
    assert live.message_id in fake.messages[live.channel_id]
    assert live.channel_id in fake.channels_by_id


def test_uploads_alternate_between_channels():
    from secret.core.sync_engine import interleave_by_channel

    uploads = ["사진/여행/a.jpg", "사진/여행/b.jpg", "사진/여행/c.jpg", "사진/일상/d.jpg", "영상/e.mp4"]
    assert interleave_by_channel(uploads) == ["사진/여행/a.jpg", "사진/일상/d.jpg", "영상/e.mp4", "사진/여행/b.jpg", "사진/여행/c.jpg"]


def test_config_is_not_rewritten_for_every_file(env, monkeypatch):
    root, fake, store = env
    for i in range(30):
        touch(root, f"사진/{i:02d}.jpg", os.urandom(50))
    saves = []
    real = store.save
    monkeypatch.setattr(store, "save", lambda: saves.append(1) or real())
    engine, _, result = sync(root, fake, store)
    assert result.complete and len(engine.manifest.files) == 30
    assert len(saves) < 10
    assert store.settings.journal == {}


def test_folder_rename_uploads_nothing_and_restores_under_the_new_name(env, tmp_path):
    from secret.core.restore import restore

    root, fake, store = env
    touch(root, "사진/여행/a.jpg", os.urandom(2500))
    touch(root, "사진/일상/b.jpg", b"bbb")
    touch(root, "사진/c.jpg", b"cc")
    touch(root, "영상/d.mp4", b"dd")
    engine, _, _ = sync(root, fake, store)
    ids_before = set(fake.channels_by_id)
    uploads_before = fake.uploads

    (root / "사진").rename(root / "그림")
    engine, plan, result = sync(root, fake, store)
    assert result.complete and result.uploaded == [] and result.deleted == []
    assert fake.uploads == uploads_before + 1
    assert set(fake.channels_by_id) == ids_before
    assert set(engine.manifest.files) == {"그림/여행/a.jpg", "그림/일상/b.jpg", "그림/c.jpg", "영상/d.mp4"}
    assert "그림/여행" in engine.manifest.channels and "사진/여행" not in engine.manifest.channels

    (root / "그림/일상").rename(root / "그림/주말")
    touch(root, "그림/주말/new.jpg", b"new")
    engine, plan, result = sync(root, fake, store)
    assert result.complete and result.uploaded == ["그림/주말/new.jpg"]
    assert data_bot_messages(fake) == referenced(engine.manifest)

    out = tmp_path / "restored"
    r = restore(fake, fake.guild_id, PW, out)
    assert r.verified == r.total == 5 and not r.failed
    assert (out / "그림/주말/b.jpg").read_bytes() == b"bbb" and (out / "그림/주말/new.jpg").exists()
    assert not (out / "사진").exists()


def kept_refs(m):
    from secret.core import history

    ids = set()
    for e in history.references(m.files, m.history, m.trash).values():
        ids |= {e.message_id, *e.part_ids}
    return ids


def vsync(root, fake, store, **kw):
    return sync(root, fake, store, keep_versions=kw.pop("keep_versions", 30), keep_trash=kw.pop("keep_trash", 100), **kw)


def test_editing_keeps_the_old_content_for_earlier_versions(env):
    root, fake, store = env
    touch(root, "메모/글.txt", b"first version")
    engine, _, _ = vsync(root, fake, store)
    first = engine.manifest.files["메모/글.txt"]
    touch(root, "메모/글.txt", b"second version!!")
    engine, _, result = vsync(root, fake, store)
    assert result.complete
    assert first.message_id in fake.messages[first.channel_id]
    assert [v.id for v in engine.manifest.history] == [1, 2]
    assert engine.state_at(1).files["메모/글.txt"].message_id == first.message_id
    assert data_bot_messages(fake) == kept_refs(engine.manifest)


def test_deleted_files_go_to_trash_and_their_folder_channel_stays(env):
    root, fake, store = env
    touch(root, "영상/게임/a.mp4", b"video")
    touch(root, "사진/b.jpg", b"pic")
    engine, _, _ = vsync(root, fake, store)
    game = engine.manifest.channels["영상/게임"]
    (root / "영상/게임/a.mp4").unlink()
    (root / "영상/게임").rmdir()
    (root / "영상").rmdir()
    engine, _, result = vsync(root, fake, store)
    assert result.deleted == ["영상/게임/a.mp4"]
    m = engine.manifest
    assert [t.rel for t in m.trash] == ["영상/게임/a.mp4"]
    assert "영상/게임" not in m.channels and game in m.retired_channels
    assert game in fake.channels_by_id
    assert data_bot_messages(fake) == kept_refs(m)


def test_trash_restore_and_purge_only_rewrite_the_index(env):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"aaa")
    touch(root, "사진/b.jpg", b"bbb")
    engine, _, _ = vsync(root, fake, store)
    (root / "사진/a.jpg").unlink()
    (root / "사진/b.jpg").unlink()
    engine, _, _ = vsync(root, fake, store)
    uploads = fake.uploads
    a = next(t for t in engine.manifest.trash if t.rel == "사진/a.jpg")
    state = engine.state_at(engine.manifest.history[-1].id)
    state.files["사진/a.jpg"] = a.entry
    out = engine.commit_state(state, kind="trash-restore", untrash={a.entry.message_id})
    assert out.version.kind == "trash-restore" and "사진/a.jpg" in engine.manifest.files
    assert [t.rel for t in engine.manifest.trash] == ["사진/b.jpg"]
    b = engine.manifest.trash[0]
    engine.commit_state(engine.state_at(engine.manifest.history[-1].id), kind="trash-purge", untrash={b.entry.message_id})
    assert engine.manifest.trash == []
    assert fake.uploads - uploads == 2
    assert data_bot_messages(fake) == kept_refs(engine.manifest)
    assert b.entry.message_id in data_bot_messages(fake)
    engine.keep_versions = 1
    engine.commit_state(engine.state_at(engine.manifest.history[-1].id), kind="rollback")
    assert b.entry.message_id not in data_bot_messages(fake)
    assert data_bot_messages(fake) == kept_refs(engine.manifest)


def test_rollback_commit_brings_back_an_earlier_state(env):
    root, fake, store = env
    touch(root, "사진/a.jpg", b"aaa")
    touch(root, "영상/v.mp4", b"vvv")
    engine, _, _ = vsync(root, fake, store)
    (root / "영상/v.mp4").unlink()
    (root / "영상").rmdir()
    touch(root, "사진/a.jpg", b"changed a")
    engine, _, _ = vsync(root, fake, store)
    v1 = engine.state_at(1)
    engine.commit_state(v1, kind="rollback")
    m = engine.manifest
    assert {r: e.message_id for r, e in m.files.items()} == {r: e.message_id for r, e in v1.files.items()}
    assert "영상/" in m.channels and m.retired_channels == {}
    assert [v.kind for v in m.history] == ["sync", "sync", "rollback"]
    assert data_bot_messages(fake) == kept_refs(m)


def test_old_versions_beyond_the_limit_are_cleaned_up(env):
    root, fake, store = env
    for i in range(6):
        touch(root, "메모/글.txt", f"version {i}".encode() * (i + 1))
        engine, _, result = vsync(root, fake, store, keep_versions=3)
        assert result.complete
    m = engine.manifest
    assert [v.id for v in m.history] == [4, 5, 6]
    assert data_bot_messages(fake) == kept_refs(m)
    assert len([mid for mid in data_bot_messages(fake)]) == 3
