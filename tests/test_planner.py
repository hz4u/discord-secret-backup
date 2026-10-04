import hashlib
import os

from secret.core import planner, scanner
from secret.core.manifest import FileEntry, Manifest


def touch(root, rel, data=b"x", mtime_ns=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime_ns is not None:
        os.utime(p, ns=(mtime_ns, mtime_ns))
    return p


def entry_for(root, rel, channel_id="c1"):
    p = root / rel
    st = p.stat()
    return FileEntry(
        size=st.st_size, mtime_ns=st.st_mtime_ns, sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
        file_id="00" * 16, nonce_prefix="00" * 8, n=1, channel_id=channel_id, message_id="m",
        thread_id=None, part_ids=["m"],
    )


class CountingHasher:
    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path.name)
        return planner.sha256_file(path)


def backed_up(root, rels):
    m = Manifest.empty()
    for rel in rels:
        top, sub = scanner.logical_folder(rel)
        m.categories.setdefault(top, f"cat-{top}")
        m.channels.setdefault(scanner.channel_key(top, sub), f"ch-{top}/{sub}")
        m.files[rel] = entry_for(root, rel, m.channels[scanner.channel_key(top, sub)])
    m.dirs = sorted(scanner.scan(root).dirs)
    return m


def test_first_backup(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"aaaa")
    touch(tmp_path, "사진/b.png", b"bb")
    touch(tmp_path, "c.txt", b"c")
    touch(tmp_path, "x.tmp")
    hasher = CountingHasher()
    plan = planner.make_plan(Manifest.empty(), scanner.scan(tmp_path), tmp_path, hasher)
    assert sorted(plan.new) == ["c.txt", "사진/b.png", "사진/여행/a.jpg"]
    assert plan.changed == plan.deleted == plan.renamed == plan.touched == []
    assert plan.excluded == ["x.tmp"]
    assert sorted(plan.create_categories) == ["", "사진"]
    assert sorted(plan.create_channels) == ["/", "사진/", "사진/여행"]
    assert plan.upload_bytes == 7
    assert plan.channel_count_after == 2 + 3 + 2
    assert hasher.calls == []
    assert not plan.is_empty


def test_nothing_changed(tmp_path):
    touch(tmp_path, "사진/a.jpg")
    m = backed_up(tmp_path, ["사진/a.jpg"])
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.is_empty


def test_size_change_is_changed_without_hash(tmp_path):
    touch(tmp_path, "사진/a.jpg", b"one")
    m = backed_up(tmp_path, ["사진/a.jpg"])
    touch(tmp_path, "사진/a.jpg", b"longer content")
    hasher = CountingHasher()
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, hasher)
    assert plan.changed == ["사진/a.jpg"]
    assert hasher.calls == []


def test_mtime_only_is_touched(tmp_path):
    touch(tmp_path, "사진/a.jpg", b"same", mtime_ns=1_000_000_000)
    m = backed_up(tmp_path, ["사진/a.jpg"])
    touch(tmp_path, "사진/a.jpg", b"same", mtime_ns=2_000_000_000)
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.touched == ["사진/a.jpg"]
    assert plan.changed == []
    assert not plan.is_empty


def test_same_size_different_content_is_changed(tmp_path):
    touch(tmp_path, "사진/a.jpg", b"aaaa", mtime_ns=1_000_000_000)
    m = backed_up(tmp_path, ["사진/a.jpg"])
    touch(tmp_path, "사진/a.jpg", b"bbbb", mtime_ns=2_000_000_000)
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.changed == ["사진/a.jpg"]


def test_rename_in_same_channel(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"photo")
    m = backed_up(tmp_path, ["사진/여행/a.jpg"])
    (tmp_path / "사진/여행/a.jpg").rename(tmp_path / "사진/여행/b.jpg")
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.renamed == [("사진/여행/a.jpg", "사진/여행/b.jpg")]
    assert plan.new == plan.deleted == []
    assert plan.upload_bytes == 0


def test_rename_into_deeper_folder_same_channel(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"photo")
    m = backed_up(tmp_path, ["사진/여행/a.jpg"])
    (tmp_path / "사진/여행/2025").mkdir()
    (tmp_path / "사진/여행/a.jpg").rename(tmp_path / "사진/여행/2025/a.jpg")
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.renamed == [("사진/여행/a.jpg", "사진/여행/2025/a.jpg")]


def test_moving_everything_into_a_new_folder_reuses_the_channel(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"photo")
    m = backed_up(tmp_path, ["사진/여행/a.jpg"])
    (tmp_path / "사진/일상").mkdir()
    (tmp_path / "사진/여행/a.jpg").rename(tmp_path / "사진/일상/a.jpg")
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.new == [] and plan.deleted == []
    assert plan.renamed == [("사진/여행/a.jpg", "사진/일상/a.jpg")]
    assert plan.remap_channels == {"사진/일상": "사진/여행"}
    assert plan.create_channels == [] and plan.delete_channels == []


def test_delete_folder_removes_channel_and_category(tmp_path):
    touch(tmp_path, "영상/게임/a.mp4")
    touch(tmp_path, "사진/b.jpg")
    m = backed_up(tmp_path, ["영상/게임/a.mp4", "사진/b.jpg"])
    (tmp_path / "영상/게임/a.mp4").unlink()
    (tmp_path / "영상/게임").rmdir()
    (tmp_path / "영상").rmdir()
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.deleted == ["영상/게임/a.mp4"]
    assert plan.delete_channels == ["영상/게임"]
    assert plan.delete_categories == ["영상"]


def test_ambiguous_folder_renames_upload_again_instead_of_guessing(tmp_path):
    t = 1_700_000_000_000_000_000
    touch(tmp_path, "사진/A/a.png", b"AAAA", t)
    touch(tmp_path, "사진/B/a.png", b"BBBB", t)
    m = backed_up(tmp_path, ["사진/A/a.png", "사진/B/a.png"])
    for old, new in (("A", "C"), ("B", "D")):
        (tmp_path / "사진" / old).rename(tmp_path / "사진" / new)
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.remap_channels == {} and plan.renamed == []
    assert sorted(plan.new) == ["사진/C/a.png", "사진/D/a.png"]
    assert sorted(plan.deleted) == ["사진/A/a.png", "사진/B/a.png"]


def test_clear_folder_rename_still_uploads_nothing_with_a_shared_duplicate(tmp_path):
    touch(tmp_path, "사진/A/a.png", b"same", 1)
    touch(tmp_path, "사진/A/b.png", b"bbbb", 2)
    touch(tmp_path, "사진/A/c.png", b"cccc", 3)
    m = backed_up(tmp_path, ["사진/A/a.png", "사진/A/b.png", "사진/A/c.png"])
    (tmp_path / "사진/A").rename(tmp_path / "사진/A2")
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.remap_channels == {"사진/A2": "사진/A"} and plan.new == [] and plan.deleted == []


def test_files_in_an_unreadable_folder_are_not_deleted(tmp_path):
    touch(tmp_path, "TOP secret/xxxx/a.jpg")
    touch(tmp_path, "TOP secret/xxxx/깊은/b.jpg")
    touch(tmp_path, "일반/c.jpg")
    m = backed_up(tmp_path, ["TOP secret/xxxx/a.jpg", "TOP secret/xxxx/깊은/b.jpg", "일반/c.jpg"])
    scan = scanner.scan(tmp_path)
    for rel in [r for r in scan.files if r.startswith("TOP secret/xxxx/")]:
        del scan.files[rel]
    scan.dirs.remove("TOP secret/xxxx/깊은")
    scan.unreadable.append("TOP secret/xxxx")
    plan = planner.make_plan(m, scan, tmp_path, CountingHasher())
    assert plan.deleted == [] and plan.delete_channels == [] and plan.delete_categories == []
    assert plan.unseen == ["TOP secret/xxxx/a.jpg", "TOP secret/xxxx/깊은/b.jpg"]
    assert "TOP secret/xxxx/깊은" in plan.dirs and plan.is_empty
    assert planner.quick_pending(m, scan).deleted == 0


def test_unreadable_root_deletes_nothing(tmp_path):
    touch(tmp_path, "사진/a.jpg")
    m = backed_up(tmp_path, ["사진/a.jpg"])
    plan = planner.make_plan(m, scanner.ScanResult(unreadable=["."]), tmp_path, CountingHasher())
    assert plan.deleted == [] and plan.delete_channels == [] and plan.unseen == ["사진/a.jpg"]


def test_only_candidates_with_matching_size_are_hashed(tmp_path):
    touch(tmp_path, "사진/a.jpg", b"12345")
    m = backed_up(tmp_path, ["사진/a.jpg"])
    (tmp_path / "사진/a.jpg").unlink()
    touch(tmp_path, "사진/big.jpg", b"1234567890")
    touch(tmp_path, "사진/same_size.jpg", b"abcde")
    hasher = CountingHasher()
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, hasher)
    assert hasher.calls == ["same_size.jpg"]
    assert sorted(plan.new) == ["사진/big.jpg", "사진/same_size.jpg"]
    assert plan.deleted == ["사진/a.jpg"]


def test_empty_dir_change_is_not_empty_plan(tmp_path):
    touch(tmp_path, "사진/a.jpg")
    m = backed_up(tmp_path, ["사진/a.jpg"])
    (tmp_path / "새폴더").mkdir()
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, CountingHasher())
    assert plan.dirs_changed and not plan.is_empty


def test_quick_pending(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"a")
    touch(tmp_path, "사진/여행/b.jpg", b"b")
    m = backed_up(tmp_path, ["사진/여행/a.jpg", "사진/여행/b.jpg"])
    touch(tmp_path, "사진/여행/a.jpg", b"changed!")
    (tmp_path / "사진/여행/b.jpg").unlink()
    touch(tmp_path, "사진/새.jpg")
    touch(tmp_path, "x.tmp")
    p = planner.quick_pending(m, scanner.scan(tmp_path))
    assert (p.new, p.changed, p.deleted, p.excluded) == (1, 1, 1, 1)
    assert p.per_folder["사진"] == 3
    assert p.per_folder["사진/여행"] == 2
    assert p.total == 3


def test_forced_file_is_changed_even_if_identical(tmp_path):
    touch(tmp_path, "사진/a.jpg", b"a")
    touch(tmp_path, "사진/b.jpg", b"b")
    m = backed_up(tmp_path, ["사진/a.jpg", "사진/b.jpg"])
    scan = scanner.scan(tmp_path)
    plan = planner.make_plan(m, scan, tmp_path, force={"사진/a.jpg"})
    assert plan.changed == ["사진/a.jpg"] and not plan.new and not plan.touched
    assert planner.quick_pending(m, scan, force={"사진/a.jpg"}).changed == 1


def rename_dir(root, old, new):
    (root / old).rename(root / new)


def test_top_folder_rename_changes_only_the_index(tmp_path):
    rels = ["사진/여행/a.jpg", "사진/일상/b.jpg", "사진/c.jpg"]
    for i, rel in enumerate(rels):
        touch(tmp_path, rel, bytes([i]) * (i + 3))
    m = backed_up(tmp_path, rels)
    rename_dir(tmp_path, "사진", "그림")
    hasher = CountingHasher()
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, hasher)
    assert plan.new == [] and plan.deleted == [] and plan.upload_bytes == 0
    assert sorted(plan.renamed) == [("사진/c.jpg", "그림/c.jpg"), ("사진/여행/a.jpg", "그림/여행/a.jpg"),
                                    ("사진/일상/b.jpg", "그림/일상/b.jpg")]
    assert plan.remap_channels == {"그림/여행": "사진/여행", "그림/일상": "사진/일상", "그림/": "사진/"}
    assert plan.remap_categories == {"그림": "사진"}
    assert plan.renamed_folders == 1
    assert plan.create_channels == plan.delete_channels == plan.create_categories == plan.delete_categories == []
    assert hasher.calls == []
    assert not plan.is_empty
    p = planner.quick_pending(m, scanner.scan(tmp_path))
    assert (p.new, p.deleted) == (0, 0)


def test_subfolder_rename_with_added_and_removed_files(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"aaa")
    touch(tmp_path, "사진/여행/b.jpg", b"bbbb")
    touch(tmp_path, "사진/일상/c.jpg", b"c")
    m = backed_up(tmp_path, ["사진/여행/a.jpg", "사진/여행/b.jpg", "사진/일상/c.jpg"])
    rename_dir(tmp_path, "사진/여행", "사진/휴가")
    (tmp_path / "사진/휴가/b.jpg").unlink()
    touch(tmp_path, "사진/휴가/new.jpg", b"new!!")
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path)
    assert plan.remap_channels == {"사진/휴가": "사진/여행"} and plan.remap_categories == {}
    assert plan.renamed_folders == 1
    assert plan.renamed == [("사진/여행/a.jpg", "사진/휴가/a.jpg")]
    assert plan.new == ["사진/휴가/new.jpg"] and plan.deleted == ["사진/여행/b.jpg"]
    assert plan.create_channels == [] and plan.delete_channels == []


def test_partial_move_is_uploaded_again(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"aaa")
    touch(tmp_path, "사진/여행/b.jpg", b"bbbb")
    touch(tmp_path, "사진/일상/c.jpg", b"c")
    m = backed_up(tmp_path, ["사진/여행/a.jpg", "사진/여행/b.jpg", "사진/일상/c.jpg"])
    (tmp_path / "사진/여행/a.jpg").rename(tmp_path / "사진/일상/a.jpg")
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path)
    assert plan.remap_channels == {} and plan.new == ["사진/일상/a.jpg"] and plan.deleted == ["사진/여행/a.jpg"]


def test_subfolder_moved_to_another_existing_top_folder_is_uploaded_again(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"aaa")
    touch(tmp_path, "사진/일상/c.jpg", b"c")
    touch(tmp_path, "영상/x.mp4", b"x")
    m = backed_up(tmp_path, ["사진/여행/a.jpg", "사진/일상/c.jpg", "영상/x.mp4"])
    (tmp_path / "사진/여행").rename(tmp_path / "영상/여행")
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path)
    assert plan.remap_channels == {} and plan.new == ["영상/여행/a.jpg"]


def test_rename_with_changed_mtime_falls_back_to_hash(tmp_path):
    touch(tmp_path, "사진/여행/a.jpg", b"aaa", mtime_ns=1_000_000_000)
    m = backed_up(tmp_path, ["사진/여행/a.jpg"])
    rename_dir(tmp_path, "사진", "그림")
    os.utime(tmp_path / "그림/여행/a.jpg", ns=(2_000_000_000, 2_000_000_000))
    hasher = CountingHasher()
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path, hasher)
    assert plan.remap_channels == {"그림/여행": "사진/여행"}
    assert plan.renamed == [("사진/여행/a.jpg", "그림/여행/a.jpg")] and hasher.calls == ["a.jpg"]


def test_channel_count_includes_retired_channels(tmp_path):
    touch(tmp_path, "사진/a.jpg", b"a")
    m = backed_up(tmp_path, ["사진/a.jpg"])
    m.retired_channels = {"old1": "oldcat", "old2": "oldcat"}
    m.retired_categories = ["oldcat"]
    plan = planner.make_plan(m, scanner.scan(tmp_path), tmp_path)
    assert plan.channel_count_after == 2 + 1 + 1 + 3
