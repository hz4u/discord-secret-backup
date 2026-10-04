import copy

from secret.core import history
from secret.core.history import advance, state_at
from secret.core.manifest import FileEntry, Manifest

T = "2026-09-30T10:00:00"


def fe(msg: str, sha: str | None = None, channel: str = "c1") -> FileEntry:
    return FileEntry(size=1, mtime_ns=1, sha256=sha or f"sha-{msg}", file_id="00" * 16, nonce_prefix="00" * 8, n=1,
                     channel_id=channel, message_id=msg, thread_id=None, part_ids=[msg])


def current(m: Manifest, files: dict, channels=None, categories=None, dirs=None) -> Manifest:
    new = copy.deepcopy(m)
    new.files = dict(files)
    if channels is not None:
        new.channels = dict(channels)
    if categories is not None:
        new.categories = dict(categories)
    if dirs is not None:
        new.dirs = list(dirs)
    return new


def base() -> Manifest:
    m = Manifest.empty()
    m.categories = {"사진": "k1"}
    m.channels = {"사진/": "c1"}
    return m


def msgs(garbage) -> set[str]:
    return {e.message_id for e in garbage.entries}


def test_every_sync_records_a_version_with_a_summary():
    m = base()
    out = advance(m, current(m, {"a.jpg": fe("m1"), "b.jpg": fe("m2")}), kind="sync", at=T)
    m = out.manifest
    assert [v.id for v in m.history] == [1] and m.history[0].summary == {"new": 2, "changed": 0, "deleted": 0, "renamed": 0}
    out = advance(m, current(m, {"a.jpg": fe("m3"), "c.jpg": fe("m2")}), kind="sync", at=T, deleted=["b.jpg"])
    m = out.manifest
    assert m.history[-1].summary == {"new": 0, "changed": 1, "deleted": 0, "renamed": 1}
    assert out.garbage.entries == []


def test_state_at_rebuilds_any_kept_version():
    m = base()
    m = advance(m, current(m, {"a.jpg": fe("m1")}), kind="sync", at=T).manifest
    m = advance(m, current(m, {"a.jpg": fe("m2"), "b.jpg": fe("m3")}), kind="sync", at=T).manifest
    m = advance(m, current(m, {"b.jpg": fe("m3")}), kind="sync", at=T, deleted=["a.jpg"]).manifest
    assert {r: e.message_id for r, e in state_at(m, 1).files.items()} == {"a.jpg": "m1"}
    assert {r: e.message_id for r, e in state_at(m, 2).files.items()} == {"a.jpg": "m2", "b.jpg": "m3"}
    assert {r: e.message_id for r, e in state_at(m, 3).files.items()} == {"b.jpg": "m3"}


def test_only_the_last_n_versions_are_kept_and_the_rest_is_garbage():
    m = base()
    for i in range(5):
        m = advance(m, current(m, {"a.jpg": fe(f"m{i}")}), kind="sync", at=T, keep_versions=3).manifest
    assert [v.id for v in m.history] == [3, 4, 5]
    out = advance(m, current(m, {"a.jpg": fe("m5")}), kind="sync", at=T, keep_versions=3)
    assert msgs(out.garbage) == {"m2"}


def test_keep_zero_behaves_like_before():
    m = base()
    m = advance(m, current(m, {"a.jpg": fe("m1"), "b.jpg": fe("m2")}), kind="sync", at=T, keep_versions=0, keep_trash=0).manifest
    out = advance(m, current(m, {"a.jpg": fe("m3")}), kind="sync", at=T, deleted=["b.jpg"], keep_versions=0, keep_trash=0)
    assert msgs(out.garbage) == {"m1", "m2"}
    assert out.manifest.history == [] and out.manifest.trash == []


def test_trash_keeps_whole_batches_up_to_the_limit():
    m = base()
    files = {f"f{i}.jpg": fe(f"m{i}") for i in range(8)}
    m = advance(m, current(m, files), kind="sync", at=T).manifest
    m = advance(m, current(m, {k: v for k, v in files.items() if k not in ("f0.jpg", "f1.jpg", "f2.jpg")}),
                kind="sync", at=T, deleted=["f0.jpg", "f1.jpg", "f2.jpg"], keep_trash=4).manifest
    assert sorted(t.rel for t in m.trash) == ["f0.jpg", "f1.jpg", "f2.jpg"]
    rest = {k: v for k, v in files.items() if k not in ("f0.jpg", "f1.jpg", "f2.jpg", "f3.jpg", "f4.jpg", "f5.jpg", "f6.jpg")}
    out = advance(m, current(m, rest), kind="sync", at=T, deleted=["f3.jpg", "f4.jpg", "f5.jpg", "f6.jpg"], keep_trash=4,
                  keep_versions=0)
    assert sorted(t.rel for t in out.manifest.trash) == ["f3.jpg", "f4.jpg", "f5.jpg", "f6.jpg"]
    assert msgs(out.garbage) == {"m0", "m1", "m2"}


def test_newest_batch_is_kept_whole_even_if_bigger_than_the_limit():
    m = base()
    files = {f"f{i}.jpg": fe(f"m{i}") for i in range(6)}
    m = advance(m, current(m, files), kind="sync", at=T).manifest
    out = advance(m, current(m, {}), kind="sync", at=T, deleted=list(files), keep_trash=4)
    assert len(out.manifest.trash) == 6


def test_moved_content_does_not_go_to_trash():
    m = base()
    m = advance(m, current(m, {"a.jpg": fe("m1", sha="same")}), kind="sync", at=T).manifest
    out = advance(m, current(m, {"다른/a.jpg": fe("m2", sha="same")}), kind="sync", at=T, deleted=["a.jpg"])
    assert out.manifest.trash == []


def test_untrash_removes_items_and_frees_them():
    m = base()
    m = advance(m, current(m, {"a.jpg": fe("m1")}), kind="sync", at=T).manifest
    m = advance(m, current(m, {}), kind="sync", at=T, deleted=["a.jpg"], keep_versions=0).manifest
    assert [t.rel for t in m.trash] == ["a.jpg"]
    out = advance(m, current(m, {}), kind="trash-purge", at=T, untrash={"m1"}, keep_versions=0)
    assert out.manifest.trash == [] and msgs(out.garbage) == {"m1"}
    assert out.version is None


def test_folder_channel_is_retired_while_history_uses_it_then_deleted():
    m = base()
    m.categories = {"사진": "k1", "영상": "k2"}
    m.channels = {"사진/": "c1", "영상/": "c2"}
    m = advance(m, current(m, {"사진/a.jpg": fe("m1", channel="c1"), "영상/v.mp4": fe("m2", channel="c2")}),
                kind="sync", at=T, keep_versions=2).manifest
    out = advance(m, current(m, {"사진/a.jpg": fe("m1", channel="c1")}, channels={"사진/": "c1"}, categories={"사진": "k1"}),
                  kind="sync", at=T, deleted=["영상/v.mp4"], keep_versions=2)
    m = out.manifest
    assert m.retired_channels == {"c2": "k2"} and m.retired_categories == ["k2"]
    assert out.garbage.channels == [] and out.garbage.categories == []
    gone_channels, gone_categories, gone = [], [], set()
    for _ in range(2):
        out = advance(m, current(m, m.files), kind="sync", at=T, untrash={"m2"}, keep_versions=2)
        m = out.manifest
        gone_channels += out.garbage.channels
        gone_categories += out.garbage.categories
        gone |= msgs(out.garbage)
    assert m.retired_channels == {} and m.retired_categories == []
    assert gone_channels == ["c2"] and gone_categories == ["k2"] and "m2" in gone


def test_channel_budget_trims_old_versions():
    m = Manifest.empty()
    m.categories = {"k": "cat"}
    m.channels = {f"k/{i}": f"c{i}" for i in range(6)}
    files = {f"k/{i}/a.jpg": fe(f"m{i}", channel=f"c{i}") for i in range(6)}
    m = advance(m, current(m, files), kind="sync", at=T).manifest
    out = advance(m, current(m, {"k/0/a.jpg": files["k/0/a.jpg"]}, channels={"k/0": "c0"}), kind="sync", at=T,
                  deleted=[f"k/{i}/a.jpg" for i in range(1, 6)], keep_trash=0, channel_budget=2 + 2 + 2)
    assert out.trimmed_versions >= 1
    assert len(out.manifest.retired_channels) + 2 + 2 <= 6
    assert set(out.garbage.channels) <= {f"c{i}" for i in range(1, 6)} and out.garbage.channels


def test_rollback_revives_retired_channels():
    m = base()
    m.categories = {"사진": "k1", "영상": "k2"}
    m.channels = {"사진/": "c1", "영상/": "c2"}
    both = {"사진/a.jpg": fe("m1", channel="c1"), "영상/v.mp4": fe("m2", channel="c2")}
    m = advance(m, current(m, both), kind="sync", at=T).manifest
    m = advance(m, current(m, {"사진/a.jpg": both["사진/a.jpg"]}, channels={"사진/": "c1"}, categories={"사진": "k1"}),
                kind="sync", at=T, deleted=["영상/v.mp4"]).manifest
    assert "c2" in m.retired_channels
    target = state_at(m, 1)
    out = advance(m, current(m, target.files, channels=target.channels, categories=target.categories), kind="rollback", at=T)
    assert out.manifest.retired_channels == {} and out.manifest.channels == {"사진/": "c1", "영상/": "c2"}
    assert out.garbage.entries == [] and out.garbage.channels == []


def test_history_from_before_an_old_exe_sync_is_dropped():
    m = base()
    m = advance(m, current(m, {"a.jpg": fe("m1")}), kind="sync", at=T).manifest
    m = advance(m, current(m, {}), kind="sync", at=T, deleted=["a.jpg"]).manifest
    m.files = {"x.jpg": fe("m9")}
    out = advance(m, current(m, {"x.jpg": fe("m9"), "y.jpg": fe("m10")}), kind="sync", at=T)
    assert [v.id for v in out.manifest.history] == [3]
    assert [t.rel for t in out.manifest.trash] == ["a.jpg"]


def test_digest_is_stable():
    assert history.state_digest({"a": fe("m1"), "b": fe("m2")}) == history.state_digest({"b": fe("m2"), "a": fe("m1")})
