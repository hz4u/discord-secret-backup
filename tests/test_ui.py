import os

import pytest

pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402

from secret.ui import theme  # noqa: E402
from secret.ui.dialogs import sync_confirm, unlock  # noqa: E402
from secret.ui.main_window import MainWindow  # noqa: E402
from secret.ui.session import Session  # noqa: E402
from tests.fake_discord import FakeDiscord  # noqa: E402

PW = "central passphrase for secret"


@pytest.fixture
def usb(tmp_path):
    root = tmp_path / "usb"
    for rel, data in [("사진/여행/바다.jpg", b"jpg"), ("사진/일상.png", b"png"), ("설치.tmp", b"exe")]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


@pytest.fixture
def window(qtbot, qapp, usb, monkeypatch):
    theme.install(qapp)
    monkeypatch.setattr(unlock.UnlockDialog, "exec", lambda self: unlock.UnlockDialog.Rejected)
    fake = FakeDiscord()
    w = MainWindow(Session(usb, client_factory=lambda token: fake))
    w.fake = fake
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: bool(w.session.scan.files))
    return w


def names(w):
    return [it.name for it in w.gallery.model.items]


def test_gallery_navigation(window):
    g = window.gallery
    assert names(window) == ["사진", "설치.tmp"]
    assert g.model.items[1].excluded
    g.set_folder("사진")
    assert names(window) == ["여행", "일상.png"]
    g.set_folder("사진/여행")
    assert names(window) == ["바다.jpg"]
    crumbs = [g.crumbs.itemAt(i).widget() for i in range(g.crumbs.count())]
    assert [c.text() for c in crumbs if hasattr(c, "text") and c.text()] == ["USB", "사진", "여행"]
    g.go_back()
    assert g.current == "사진"


def test_back_and_forward_navigation(window):
    g = window.gallery
    assert not g.back.isEnabled() and not g.forward.isEnabled()
    g.set_folder("사진")
    g.set_folder("사진/여행")
    g.go_back()
    assert g.current == "사진"
    assert g.forward.isEnabled()
    g.go_back()
    assert g.current == ""
    g.go_forward()
    g.go_forward()
    assert g.current == "사진/여행"
    assert not g.forward.isEnabled()
    g.go_back()
    g.set_folder("")
    assert not g.forward.isEnabled()
    g2 = window.gallery
    g2.back_stack.clear()
    g2.forward_stack.clear()
    g2.set_folder("사진/여행", remember=False)
    g2.go_back()
    assert g2.current == "사진"
    g2.go_forward()
    assert g2.current == "사진/여행"


def test_mouse_side_buttons_navigate(window, qtbot):
    g = window.gallery
    window.show()
    g.set_folder("사진")
    qtbot.mouseClick(g.grid.viewport(), Qt.BackButton)
    assert g.current == ""
    qtbot.mouseClick(g.grid.viewport(), Qt.ForwardButton)
    assert g.current == "사진"


def test_kind_filter_and_search(window):
    g = window.gallery
    g.set_kind("image")
    assert sorted(names(window)) == ["바다.jpg", "일상.png"]
    g.set_kind(None)
    g.search.setText("바다")
    assert names(window) == ["바다.jpg"]


def test_full_sync_flow(window, qtbot, monkeypatch):
    s = window.session
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail, token="t", guild_id=window.fake.guild_id,
                   guild_name="테스트 서버", bot_name="bot")
    qtbot.waitUntil(lambda: bool(done))
    assert s.connected and window.right.status.text().count("연결됨")
    qtbot.waitUntil(lambda: s.pending is not None and s.pending.new == 2)

    monkeypatch.setattr(sync_confirm.SyncConfirmDialog, "exec", lambda self: sync_confirm.SyncConfirmDialog.Accepted)
    window.start_sync()
    qtbot.waitUntil(lambda: not s.sync_running, timeout=15000)
    assert window.right.sync_button.text() == "동기화 완료"
    qtbot.waitUntil(lambda: window.right.counts["new"].text() == "0")
    assert window.right.sync_button.text() == "동기화 완료"
    assert "동기화를 마쳤습니다" in window.right.log_view.toPlainText()
    qtbot.waitUntil(lambda: window.right.sync_button.text() == "지금 동기화", timeout=5000)
    assert s.settings.last_sync
    qtbot.waitUntil(lambda: s.pending.total == 0)
    files = {it.name: it.state for it in window.gallery.model.items if not it.is_dir}
    assert files == {"설치.tmp": "excluded"}
    window.gallery.set_folder("사진")
    assert {it.name: it.state for it in window.gallery.model.items if not it.is_dir} == {"일상.png": "synced"}


def test_connect_while_locked_attaches_instead_of_recreating(window, qtbot, monkeypatch):
    from secret.ui import main_window as mw

    s = window.session
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    dk_before = s.settings.dk
    s.lock()

    def unlock(reason=""):
        ok = []
        s.unlock_async(PW, lambda: ok.append(1), pytest.fail)
        qtbot.waitUntil(lambda: bool(ok))
        return True

    seen = []
    monkeypatch.setattr(window, "ensure_unlocked", unlock)
    monkeypatch.setattr(mw.ConnectDialog, "exec", lambda self: seen.append(self.mode) or mw.ConnectDialog.Rejected)
    window.open_connect()
    assert seen == ["attach"]
    assert s.settings.dk == dk_before


def test_server_icon_shows_right_after_connect(window, qtbot, monkeypatch):
    from secret.ui.dialogs.connect import ConnectDialog

    png = _png_bytes()
    fake = window.fake
    real_guild = fake.guild
    monkeypatch.setattr(fake, "guild", lambda gid: {**real_guild(gid), "icon": "abc"})
    monkeypatch.setattr(fake, "download", lambda url: png)
    s = window.session
    d = ConnectDialog(window, s, "new")
    qtbot.addWidget(d)
    d.token.edit.setText("TOKEN")
    d.guild.setText(fake.guild_id)
    d._check()
    qtbot.waitUntil(lambda: d.info is not None)
    d.passwords.pw1.edit.setText(PW)
    d.passwords.pw2.edit.setText(PW)
    d._save()
    qtbot.waitUntil(lambda: s.connected)
    assert s.settings.guild_icon and s.settings.guild_icon_hash == "abc"
    assert window.right.avatar.pixmap().cacheKey() != window.right.default_avatar.cacheKey()


def _png_bytes() -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(64, 64, QImage.Format_ARGB32)
    img.fill(QColor("#E9A23B"))
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _sample_jpg(path):
    from PySide6.QtGui import QColor, QImage

    img = QImage(640, 400, QImage.Format_RGB32)
    img.fill(QColor("#3B82F6"))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "JPG")


def test_thumbnails_stay_in_memory_only(qtbot, qapp, tmp_path, monkeypatch):
    theme.install(qapp)
    monkeypatch.setattr(unlock.UnlockDialog, "exec", lambda self: unlock.UnlockDialog.Rejected)
    root = tmp_path / "usb"
    _sample_jpg(root / "사진/a.jpg")
    old_cache = root / ".secret" / "thumbs"
    old_cache.mkdir(parents=True)
    (old_cache / "old.jpg").write_bytes(b"old thumbnail")
    (root / ".secret" / "config.dat").write_bytes(b"keep me")

    w = MainWindow(Session(root, client_factory=lambda token: FakeDiscord()))
    qtbot.addWidget(w)
    assert not old_cache.exists()
    assert (root / ".secret" / "config.dat").read_bytes() == b"keep me"

    qtbot.waitUntil(lambda: bool(w.session.scan.files))
    w.gallery.set_folder("사진")
    item = w.gallery.model.items[0]
    w.gallery.thumbs.get(item)
    qtbot.waitUntil(lambda: w.gallery.thumbs.cached(item) is not None)
    assert not old_cache.exists()
    assert sorted(p.name for p in root.rglob("*")) == sorted(["사진", "a.jpg", ".secret", "config.dat"])


def test_thumbnail_memory_is_bounded(qtbot, qapp, tmp_path):
    from secret.ui.gallery import Item, ThumbCache

    theme.install(qapp)
    for i in range(6):
        _sample_jpg(tmp_path / f"{i}.jpg")
    cache = ThumbCache(Session(tmp_path), limit=3)
    items = [Item(f"{i}.jpg", f"{i}.jpg", False, 1, i) for i in range(6)]
    for it in items:
        cache.get(it)
        qtbot.waitUntil(lambda it=it: cache.cached(it) is not None)
    assert len(cache.pixmaps) <= 3
    cache.clear()
    assert not cache.pixmaps


def test_video_thumbnail_from_first_scenes(qtbot, qapp, tmp_path):
    import shutil
    from pathlib import Path

    from secret.ui.gallery import Item, ThumbCache

    theme.install(qapp)
    shutil.copy(Path(__file__).parent / "fixtures" / "sunset.mp4", tmp_path / "노을.mp4")
    (tmp_path / "깨진.mp4").write_bytes(b"not a video")
    cache = ThumbCache(Session(tmp_path))
    good = Item("노을.mp4", "노을.mp4", False, 1, 1, kind="video")
    bad = Item("깨진.mp4", "깨진.mp4", False, 2, 2, kind="video")
    cache.get(good)
    cache.get(bad)
    qtbot.waitUntil(lambda: cache.cached(good) is not None, timeout=15000)
    assert cache.cached(good).width() > 0
    qtbot.waitUntil(lambda: cache._key(bad) in cache._failed, timeout=15000)
    assert list(tmp_path.iterdir()) and not (tmp_path / ".secret").exists()


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows 파일 속성")
def test_hide_top_folder_flow(window, qtbot, monkeypatch):
    from secret.core.hidden import is_hidden_or_system
    from secret.ui import gallery as gallery_mod
    from secret.ui.dialogs.hide_folders import HideFoldersPanel
    from secret.ui.gallery import Item

    s = window.session
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    qtbot.waitUntil(lambda: "사진" in s.scan.dirs)
    panel = HideFoldersPanel(s)
    qtbot.addWidget(panel)

    def dirs():
        return set(s.scan.dirs)

    from secret.ui.dialogs.hide_folders import set_hidden

    def toggle(rel, hide):
        set_hidden(s, rel, hide)
        s.settings_saved()
        s.refresh_scan()

    panel._toggle = toggle
    panel._toggle("사진", True)
    assert is_hidden_or_system(s.root / "사진")
    assert s.settings.hidden_folders == ["사진"]
    qtbot.waitUntil(lambda: "사진" in dirs())
    assert [it for it in window.gallery.model.items if it.name == "사진"][0].hidden

    s.lock()
    qtbot.waitUntil(lambda: "사진" not in dirs())
    ok = []
    s.unlock_async(PW, lambda: ok.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(ok) and "사진" in dirs())

    monkeypatch.setattr(gallery_mod, "QInputDialog", type("D", (), {"getText": staticmethod(lambda *a, **k: ("그림", True))}))
    window.gallery.rename(Item("사진", "사진", True))
    assert s.settings.hidden_folders == ["그림"]
    assert is_hidden_or_system(s.root / "그림")
    qtbot.waitUntil(lambda: "그림" in dirs())

    panel._toggle("그림", False)
    assert not is_hidden_or_system(s.root / "그림")
    assert s.settings.hidden_folders == []

    panel._toggle("그림/여행", True)
    qtbot.waitUntil(lambda: "그림/여행" in dirs())
    monkeypatch.setattr(gallery_mod, "QInputDialog", type("D", (), {"getText": staticmethod(lambda *a, **k: ("사진첩", True))}))
    window.gallery.rename(Item("그림", "그림", True))
    assert s.settings.hidden_folders == ["사진첩/여행"]
    assert is_hidden_or_system(s.root / "사진첩/여행")
    qtbot.waitUntil(lambda: "사진첩/여행" in dirs())

    real = HideFoldersPanel(s)
    qtbot.addWidget(real)
    item = next(i for i in real._items() if real._rel(i) == "사진첩/여행")
    item.setCheckState(0, Qt.Unchecked)
    assert s.settings.hidden_folders == []
    assert not is_hidden_or_system(s.root / "사진첩/여행")


def test_busy_log_line_animates_then_settles(window, qtbot):
    right = window.right

    def last():
        return right.log_view.toPlainText().splitlines()[-1]

    window.session.log("변경사항을 확인하는 중", "busy")
    first = last()
    assert "변경사항을 확인하는 중" in first and first[-1] in "|/-\\"
    qtbot.waitUntil(lambda: last() != first, timeout=3000)
    assert last()[-1] in "|/-\\"
    window.session.log("최신 버전입니다")
    lines = right.log_view.toPlainText().splitlines()
    assert lines[-2].endswith("변경사항을 확인하는 중")
    assert lines[-1].endswith("최신 버전입니다")
    assert not right._busy_timer.isActive()


def test_no_password_prompt_on_start(qtbot, qapp, usb, monkeypatch):
    from secret.core.config_store import ConfigStore, Settings

    theme.install(qapp)
    ConfigStore.create(usb / ".secret" / "config.dat", PW, Settings(dk=bytes(32)))
    shown = []
    monkeypatch.setattr(unlock.UnlockDialog, "exec", lambda self: shown.append(1) or unlock.UnlockDialog.Rejected)
    w = MainWindow(Session(usb, client_factory=lambda token: FakeDiscord()))
    qtbot.addWidget(w)
    w.show()
    qtbot.wait(800)
    assert shown == []
    w.right.discord_action.click()
    assert shown == [1]


def test_tools_page_in_center(window, qtbot):
    s = window.session
    window.sidebar.tools_button.click()
    assert window.center.currentWidget() is window.tools_page
    assert window.sidebar.kind_group.checkedButton() is None
    assert window.tools_page.locked_button.text() == "메인 비밀번호 만들기"
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    assert window.tools_page.stack.currentWidget() is window.tools_page.tabs
    assert window.tools_page.tabs.count() == 8
    window.sidebar.kind_buttons["image"].click()
    assert window.center.currentWidget() is window.gallery
    assert not window.sidebar.tools_button.isChecked()
    assert window.sidebar.kind_group.checkedButton() is window.sidebar.kind_buttons["image"]


def test_usb_unplug_locks(window, qtbot, tmp_path):
    s = window.session
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    real = s.root
    s.root = tmp_path / "gone"
    window._watch_usb()
    qtbot.waitUntil(lambda: not s.unlocked)
    assert not s.usb_present
    s.root = real
    window._watch_usb()
    qtbot.waitUntil(lambda: s.usb_present)


def test_usb_check_does_not_block_the_window(window, qtbot, monkeypatch):
    import threading as _threading
    from pathlib import Path

    gate = _threading.Event()
    calls = []
    s = window.session

    class SlowRoot(type(Path())):
        def exists(self, *a, **k):
            calls.append(1)
            gate.wait(5)
            return True

    s.root = SlowRoot(str(s.root))
    start = _threading.get_ident()
    window._watch_usb()
    window._watch_usb()
    assert _threading.get_ident() == start and len(calls) <= 1
    gate.set()
    qtbot.waitUntil(lambda: not window._usb_checking)
    assert len(calls) == 1


def _png(path, color="#808080", size=(64, 48)):
    from PySide6.QtGui import QColor, QImage

    img = QImage(*size, QImage.Format_RGB32)
    img.fill(QColor(color))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "PNG")


def _open(window, qtbot, folder, name):
    g = window.gallery
    window.session.refresh_scan()
    qtbot.waitUntil(lambda: any(i.rel == f"{folder}/{name}" for i in [*window.session.scan.files.values()]))
    g.set_folder(folder)
    g.refresh()
    row = next(r for r, it in enumerate(g.model.items) if it.name == name)
    g._activate(g.model.index(row, 0))


def test_viewer_opens_and_steps_in_gallery_order(window, qtbot):
    root = window.session.root
    _png(root / "사진/a.png", "#ff0000")
    _png(root / "사진/b.png", "#00ff00")
    (root / "사진/c.txt").write_bytes("메모".encode("utf-8"))
    window.gallery.sort.setCurrentIndex(2)
    _open(window, qtbot, "사진", "a.png")
    v = window.viewer
    assert window.center.currentWidget() is v
    assert v.siblings == ["사진/a.png", "사진/b.png", "사진/c.txt", "사진/일상.png"]
    assert v.count.text() == "1 / 4" and not v.prev.isEnabled()
    qtbot.waitUntil(lambda: not v.image.item.pixmap().isNull())
    assert v.body.currentWidget() is v.image and "64 × 48" in v.footer.text()
    v.step(1)
    assert v.current == "사진/b.png"
    v.step(1)
    assert v.body.currentWidget() is v.text_box and v.text.toPlainText() == "메모"
    assert v.save_btn.isVisibleTo(v) and not v.save_btn.isEnabled()
    v.step(1)
    qtbot.waitUntil(lambda: v.body.currentWidget() is v.message)
    assert not v.next.isEnabled()


def test_viewer_back_then_forward_reopens_file(window, qtbot):
    root = window.session.root
    _png(root / "사진/a.png")
    _open(window, qtbot, "사진", "a.png")
    window.go_back()
    assert window.center.currentWidget() is window.gallery
    assert window.viewer.current is None
    assert window.gallery.forward.isEnabled()
    window.go_forward()
    assert window.center.currentWidget() is window.viewer and window.viewer.current == "사진/a.png"
    window.go_back()
    window.gallery.set_folder("")
    assert not window.gallery.forward.isEnabled()


def test_text_edit_save_and_unsaved_prompt(window, qtbot, monkeypatch):
    from secret.ui import viewer as viewer_mod

    root = window.session.root
    (root / "문서").mkdir()
    note = root / "문서/메모.txt"
    note.write_bytes("하나\r\n둘".encode("cp949"))
    _open(window, qtbot, "문서", "메모.txt")
    v = window.viewer
    assert v.footer.text().startswith("CP949 · CRLF")
    v.text.moveCursor(v.text.textCursor().MoveOperation.End)
    v.text.insertPlainText("\n셋")
    assert v.dirty and v.dirty_mark.isVisibleTo(v) and v.save_btn.isEnabled()

    answers = ["cancel"]
    monkeypatch.setattr(viewer_mod, "ask_save", lambda *a: answers.pop(0))
    window.go_back()
    assert window.center.currentWidget() is v
    answers.append("discard")
    window.go_back()
    assert window.center.currentWidget() is window.gallery
    assert note.read_bytes() == "하나\r\n둘".encode("cp949")

    window.go_forward()
    v.text.moveCursor(v.text.textCursor().MoveOperation.End)
    v.text.insertPlainText("\n셋")
    from PySide6.QtGui import QKeySequence, QShortcut

    save = next(sc for sc in v.findChildren(QShortcut) if sc.key() == QKeySequence(QKeySequence.Save))
    save.activated.emit()
    assert note.read_bytes() == "하나\r\n둘\r\n셋".encode("cp949")
    assert not v.dirty and not v.dirty_mark.isVisibleTo(v)
    assert not list(note.parent.glob("*.secret-tmp"))


def test_close_window_with_unsaved_text_asks(window, qtbot, monkeypatch):
    from secret.ui import viewer as viewer_mod

    root = window.session.root
    (root / "메모.md").write_bytes(b"# title")
    window.session.refresh_scan()
    qtbot.waitUntil(lambda: "메모.md" in window.session.scan.files)
    window.gallery.refresh()
    window.open_file("메모.md", ["메모.md"])
    window.viewer.text.insertPlainText("x")
    monkeypatch.setattr(viewer_mod, "ask_save", lambda *a: "cancel")
    window.show()
    assert not window.close()
    assert window.viewer.dirty
    monkeypatch.setattr(viewer_mod, "ask_save", lambda *a: "save")
    assert window.close()
    assert (root / "메모.md").read_bytes() == b"x# title"


def test_lock_closes_viewer_on_hidden_folder_file(window, qtbot, monkeypatch):
    from secret.ui import viewer as viewer_mod
    from secret.ui.dialogs.hide_folders import set_hidden

    s = window.session
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    (s.root / "사진/비밀.txt").write_bytes(b"secret note")
    set_hidden(s, "사진", True)
    s.settings_saved()
    _open(window, qtbot, "사진", "비밀.txt")
    window.viewer.text.insertPlainText("more ")
    monkeypatch.setattr(viewer_mod, "ask_save", lambda *a: "cancel")
    window.lock()
    assert s.unlocked and window.center.currentWidget() is window.viewer
    monkeypatch.setattr(viewer_mod, "ask_save", lambda *a: "discard")
    window.lock()
    assert not s.unlocked
    assert window.center.currentWidget() is window.gallery and window.viewer.current is None


def test_viewer_closes_when_file_disappears(window, qtbot):
    root = window.session.root
    _png(root / "사진/a.png")
    _open(window, qtbot, "사진", "a.png")
    (root / "사진/a.png").unlink()
    window.session.refresh_scan()
    qtbot.waitUntil(lambda: window.center.currentWidget() is window.gallery)
    assert window.viewer.current is None


def test_broken_heic_and_large_text_show_guidance(window, qtbot, monkeypatch):
    from secret.core import textfile

    root = window.session.root
    (root / "사진/아이폰.heic").write_bytes(b"heic")
    (root / "큰.txt").write_bytes(b"x" * 20)
    monkeypatch.setattr(textfile, "MAX_EDIT_BYTES", 10)
    window.open_file("사진/아이폰.heic", [])
    v = window.viewer
    qtbot.waitUntil(lambda: v.body.currentWidget() is v.message, timeout=5000)
    assert "열 수 없습니다" in v.message_text.text()
    window.open_file("큰.txt", [])
    assert v.body.currentWidget() is v.message and "Windows 앱" in v.message_text.text()
    assert not v.save_btn.isEnabled()


def test_video_plays_and_stops_on_leave(window, qtbot, tmp_path):
    import shutil
    from pathlib import Path

    from PySide6.QtMultimedia import QMediaPlayer

    root = window.session.root
    shutil.copy(Path(__file__).parent / "fixtures" / "sunset.mp4", root / "노을.mp4")
    window.viewer.video.audio.setMuted(True)
    window.open_file("노을.mp4", ["노을.mp4"])
    v = window.viewer
    assert v.body.currentWidget() is v.video
    qtbot.waitUntil(lambda: v.video.player.duration() > 0, timeout=10000)
    assert v.video.seek.maximum() == v.video.player.duration()
    window.go_back()
    assert v.video.player.playbackState() == QMediaPlayer.StoppedState
    assert not v.video.player.source().isValid()


def test_seek_bar_jumps_to_clicked_spot(qtbot, qapp):
    from PySide6.QtCore import QPoint

    from secret.ui.viewer import ClickSlider

    theme.install(qapp)
    s = ClickSlider(Qt.Horizontal)
    qtbot.addWidget(s)
    s.setRange(0, 1000)
    s.resize(400, 24)
    s.show()
    qtbot.waitExposed(s)
    moved = []
    s.sliderMoved.connect(moved.append)
    qtbot.mouseClick(s, Qt.LeftButton, pos=QPoint(300, 12))
    assert 700 <= s.value() <= 800
    assert moved and moved[0] == s.value()


def _crumb_buttons(page):
    from PySide6.QtWidgets import QToolButton

    crumbs = page.header.crumbs
    return [w for w in (crumbs.itemAt(i).widget() for i in range(crumbs.count())) if isinstance(w, QToolButton)]


def test_viewer_header_lines_up_with_gallery(window, qtbot):
    root = window.session.root
    _png(root / "사진/a.png")
    window.show()
    qtbot.waitExposed(window)
    _open(window, qtbot, "사진", "a.png")
    qtbot.wait(50)

    def spots(page):
        header = page.header
        first_crumb = header.crumbs.itemAt(0).widget()
        return [w.mapTo(window, w.rect().topLeft()) for w in (header.back, header.forward, first_crumb)]

    in_viewer = spots(window.viewer)
    window.go_back()
    qtbot.wait(50)
    assert spots(window.gallery) == in_viewer
    window.go_forward()
    assert [b.text() for b in _crumb_buttons(window.viewer)] == ["USB", "사진", "a.png"]


def test_viewer_crumb_opens_that_folder(window, qtbot):
    root = window.session.root
    _png(root / "사진/여행/b.png")
    _open(window, qtbot, "사진/여행", "b.png")
    next(b for b in _crumb_buttons(window.viewer) if b.text() == "사진").click()
    assert window.center.currentWidget() is window.gallery
    assert window.gallery.current == "사진" and window.viewer.current is None


def test_backup_check_finds_and_repairs_a_missing_part(window, qtbot, monkeypatch):
    from secret.ui.dialogs import verify as verify_mod

    s = window.session
    fake = window.fake
    (s.root / "사진/여행/바다.jpg").write_bytes(b"x" * 3000)
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail, token="t", guild_id=fake.guild_id, guild_name="서버", bot_name="bot")
    qtbot.waitUntil(lambda: bool(done))
    monkeypatch.setattr(sync_confirm.SyncConfirmDialog, "exec", lambda self: sync_confirm.SyncConfirmDialog.Accepted)
    s.refresh_scan()
    qtbot.waitUntil(lambda: s.pending is not None and s.pending.new == 2)
    window.start_sync()
    qtbot.waitUntil(lambda: not s.sync_running and bool(s.settings.last_sync), timeout=15000)

    window.show_tools()
    tabs = window.tools_page.tabs
    panel = next(tabs.widget(i) for i in range(tabs.count()) if tabs.tabText(i) == "백업 점검")
    tabs.setCurrentWidget(panel)
    panel.quick.click()
    qtbot.waitUntil(lambda: panel.cancel is None and panel.result is not None, timeout=10000)
    assert panel.result.healthy and "모두 정상" in panel.status.text()

    entry = s.manifest().files["사진/여행/바다.jpg"]
    del fake.messages[entry.channel_id][entry.message_id]
    panel.result = None
    panel.quick.click()
    qtbot.waitUntil(lambda: panel.result is not None, timeout=10000)
    assert not panel.result.healthy and panel.problems.topLevelItemCount() == 1
    assert panel.reupload.isVisibleTo(panel) and panel.reupload.text() == "다시 올리기 (1개)"
    panel.reupload.click()
    assert s.settings.reupload == ["사진/여행/바다.jpg"] and s.pending.changed == 1

    window.start_sync()
    qtbot.waitUntil(lambda: not s.sync_running, timeout=15000)
    qtbot.waitUntil(lambda: s.settings.reupload == [])
    monkeypatch.setattr(verify_mod, "ask", lambda *a, **k: True)
    panel.result = None
    panel.deep.click()
    qtbot.waitUntil(lambda: panel.result is not None, timeout=10000)
    assert panel.result.healthy and panel.result.deep


def test_backup_check_needs_connection(window, qtbot):
    s = window.session
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    window.show_tools()
    tabs = window.tools_page.tabs
    panel = next(tabs.widget(i) for i in range(tabs.count()) if tabs.tabText(i) == "백업 점검")
    assert not panel.quick.isEnabled() and not panel.deep.isEnabled()
    assert "연결하면" in panel.status.text()


def test_sync_shows_changes_before_uploading(window, qtbot, monkeypatch):
    s = window.session
    fake = window.fake
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail, token="t", guild_id=fake.guild_id, guild_name="서버", bot_name="bot")
    qtbot.waitUntil(lambda: bool(done))
    seen = {}

    def show(dialog):
        seen["button"] = window.right.sync_button.text()
        seen["progress"] = window.right.sync_button.progress
        seen["uploads"] = fake.uploads
        return sync_confirm.SyncConfirmDialog.Rejected

    monkeypatch.setattr(sync_confirm.SyncConfirmDialog, "exec", show)
    window.start_sync()
    qtbot.waitUntil(lambda: not s.sync_running, timeout=15000)
    assert seen == {"button": "바뀐 내용 확인 중...", "progress": None, "uploads": 0}
    assert fake.uploads == 0 and window.right.sync_button.text() == "지금 동기화"


def test_nothing_to_sync_says_up_to_date(window, qtbot, monkeypatch):
    s = window.session
    fake = window.fake
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail, token="t", guild_id=fake.guild_id, guild_name="서버", bot_name="bot")
    qtbot.waitUntil(lambda: bool(done))
    monkeypatch.setattr(sync_confirm.SyncConfirmDialog, "exec", lambda self: sync_confirm.SyncConfirmDialog.Accepted)
    window.start_sync()
    qtbot.waitUntil(lambda: not s.sync_running, timeout=15000)
    shown = []
    monkeypatch.setattr(sync_confirm.SyncConfirmDialog, "exec", lambda self: shown.append(1) or 0)
    window.start_sync()
    qtbot.waitUntil(lambda: not s.sync_running, timeout=15000)
    assert shown == [] and window.right.sync_button.text() == "최신 버전입니다"


def test_confirm_dialog_ignores_instant_clicks_and_enter(qtbot, qapp):
    import time as _time

    from secret.core.planner import Plan

    theme.install(qapp)
    dlg = sync_confirm.SyncConfirmDialog(None, Plan(new=["a.jpg"], upload_bytes=10), {"a.jpg": 10}, [])
    qtbot.addWidget(dlg)
    dlg.show()
    assert not dlg.start.autoDefault() and not dlg.start.isDefault()
    dlg.start.click()
    assert dlg.result() != sync_confirm.SyncConfirmDialog.Accepted and dlg.isVisible()
    dlg._opened = _time.monotonic() - 1
    dlg.start.click()
    assert dlg.result() == sync_confirm.SyncConfirmDialog.Accepted


def test_sync_button_fills_like_a_progress_bar(qtbot, qapp):
    from secret.ui.widgets import ProgressButton

    theme.install(qapp)
    b = ProgressButton("동기화 중... 50%")
    qtbot.addWidget(b)
    b.resize(200, 40)
    b.set_progress(0.5)
    img = b.grab().toImage()
    left, right = img.pixelColor(20, 5), img.pixelColor(180, 5)
    assert left.lightness() > 200 and right.lightness() < 80
    b.set_progress(None)
    assert b.progress is None


def test_changes_made_outside_show_up_right_away(window, qtbot):
    s, g = window.session, window.gallery
    if window.watcher is None:
        pytest.skip("Windows 폴더 알림 없음")
    g.set_folder("사진")
    (s.root / "사진" / "새사진.png").write_bytes(b"png")
    qtbot.waitUntil(lambda: "사진/새사진.png" in s.scan.files, timeout=5000)
    qtbot.waitUntil(lambda: "새사진.png" in names(window), timeout=2000)

    g.set_folder("사진/여행")
    (s.root / "사진").rename(s.root / "그림")
    qtbot.waitUntil(lambda: "그림/여행/바다.jpg" in s.scan.files, timeout=5000)
    qtbot.waitUntil(lambda: g.current == "", timeout=2000)
    assert "그림" in names(window) and "사진" not in names(window)


def test_outside_changes_wait_while_restoring(window, qtbot):
    s = window.session
    if window.watcher is None:
        pytest.skip("Windows 폴더 알림 없음")
    window._restoring = True
    (s.root / "추가.jpg").write_bytes(b"x")
    qtbot.waitUntil(lambda: window._fs_pending, timeout=5000)
    assert "추가.jpg" not in s.scan.files
    window._restoring = False
    window._rescan_after_change()
    qtbot.waitUntil(lambda: "추가.jpg" in s.scan.files, timeout=5000)


def test_default_window_fits_three_cards_per_row(qtbot, qapp, usb, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from secret.ui import gallery as gallery_mod
    from secret.ui.main_window import WINDOW_SIZE

    area = QApplication.primaryScreen().availableGeometry()
    if area.width() - 40 < WINDOW_SIZE[0]:
        pytest.skip("화면이 기본 창보다 작다")
    theme.install(qapp)
    monkeypatch.setattr(unlock.UnlockDialog, "exec", lambda self: unlock.UnlockDialog.Rejected)
    w = MainWindow(Session(usb))
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(100)
    span = w.gallery.grid.width() - 2 * gallery_mod.OUTER
    assert (span + gallery_mod.GAP) // (gallery_mod.CARD_MIN + gallery_mod.GAP) >= 3


def test_thumbnails_in_hidden_folders_are_heavily_blurred(window, qtbot):
    from PySide6.QtGui import QColor, QImage

    from secret.ui.dialogs.hide_folders import set_hidden

    s, g = window.session, window.gallery
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    checker = QImage(64, 64, QImage.Format_RGB32)
    for y in range(64):
        for x in range(64):
            checker.setPixelColor(x, y, QColor("white") if (x // 4 + y // 4) % 2 else QColor("black"))
    (s.root / "비밀").mkdir()
    assert checker.save(str(s.root / "비밀" / "a.png"))
    assert checker.save(str(s.root / "보통.png"))
    set_hidden(s, "비밀", True)
    s.settings_saved()
    s.refresh_scan()
    qtbot.waitUntil(lambda: "비밀/a.png" in s.scan.files)

    def spread(pix):
        img = pix.toImage()
        values = [img.pixelColor(x, y).lightness() for y in range(0, img.height(), 3) for x in range(0, img.width(), 3)]
        return max(values) - min(values)

    g.set_folder("비밀")
    item = next(it for it in g.model.items if it.name == "a.png")
    assert item.veiled
    g.thumbs.get(item)
    qtbot.waitUntil(lambda: g.thumbs.cached(item) is not None, timeout=5000)
    original = g.thumbs.cached(item)
    assert spread(original) > 200
    assert spread(g.thumbs.veiled(item, original)) < 60

    g.set_folder("")
    plain = next(it for it in g.model.items if it.name == "보통.png")
    assert not plain.veiled


def _connected_and_synced(window, qtbot, monkeypatch):
    s, fake = window.session, window.fake
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail, token="t", guild_id=fake.guild_id, guild_name="서버", bot_name="bot")
    qtbot.waitUntil(lambda: bool(done))
    monkeypatch.setattr(sync_confirm.SyncConfirmDialog, "exec", lambda self: sync_confirm.SyncConfirmDialog.Accepted)
    _sync(window, qtbot)


def _sync(window, qtbot):
    s = window.session
    s.refresh_scan()
    qtbot.wait(300)
    window.start_sync()
    qtbot.waitUntil(lambda: not s.sync_running, timeout=15000)


def _tab(window, title):
    window.show_tools()
    tabs = window.tools_page.tabs
    panel = next(tabs.widget(i) for i in range(tabs.count()) if tabs.tabText(i) == title)
    tabs.setCurrentWidget(panel)
    return panel


def test_versions_tab_rolls_the_vault_back(window, qtbot, monkeypatch):
    from secret.ui.dialogs import timeline as timeline_mod

    s = window.session
    (s.root / "메모.txt").write_bytes(b"first")
    _connected_and_synced(window, qtbot, monkeypatch)
    (s.root / "메모.txt").write_bytes(b"second, edited")
    _sync(window, qtbot)

    panel = _tab(window, "버전 기록")
    panel.load()
    qtbot.waitUntil(lambda: panel.view.topLevelItemCount() == 2, timeout=10000)
    assert panel.view.topLevelItem(0).text(3) == "✓ 현재 버전"
    assert panel.view.topLevelItem(0).text(1) == "바뀐 파일 1개"
    assert not panel.view.header().stretchLastSection()
    panel.view.setCurrentItem(panel.view.topLevelItem(1))
    assert panel.rollback_btn.isEnabled()
    monkeypatch.setattr(timeline_mod, "ask", lambda *a, **k: True)
    panel.rollback_btn.click()
    qtbot.waitUntil(lambda: "되돌렸습니다" in panel.status.text(), timeout=15000)
    assert (s.root / "메모.txt").read_bytes() == b"first"
    assert [panel.view.topLevelItem(i).text(2) for i in range(panel.view.topLevelItemCount())][0] == "되돌리기"
    assert not s.sync_running


def test_rollback_waits_for_unsynced_changes(window, qtbot, monkeypatch):
    from secret.ui.dialogs import timeline as timeline_mod

    s = window.session
    (s.root / "메모.txt").write_bytes(b"first")
    _connected_and_synced(window, qtbot, monkeypatch)
    (s.root / "메모.txt").write_bytes(b"second")
    _sync(window, qtbot)
    (s.root / "메모.txt").write_bytes(b"not synced yet")
    panel = _tab(window, "버전 기록")
    panel.load()
    qtbot.waitUntil(lambda: panel.view.topLevelItemCount() == 2, timeout=10000)
    panel.view.setCurrentItem(panel.view.topLevelItem(1))
    monkeypatch.setattr(timeline_mod, "ask", lambda *a, **k: pytest.fail("동기화 전에는 묻지도 않는다"))
    panel.rollback_btn.click()
    qtbot.waitUntil(lambda: "먼저" in panel.status.text(), timeout=10000)
    assert (s.root / "메모.txt").read_bytes() == b"not synced yet" and panel.sync_btn.isVisibleTo(panel)


def test_trash_page_shows_deleted_files_like_a_gallery(window, qtbot, monkeypatch):
    from secret.ui import trash_page as trash_mod

    s = window.session
    _png(s.root / "사진/지울사진.png", "#ff0000")
    (s.root / "b.txt").write_bytes(b"bbb")
    window.sidebar.trash_button.click()
    assert window.center.currentWidget() is window.trash_page
    assert window.trash_page.notice_button.isVisibleTo(window.trash_page) is False

    _connected_and_synced(window, qtbot, monkeypatch)
    (s.root / "사진/지울사진.png").unlink()
    (s.root / "b.txt").unlink()
    _sync(window, qtbot)

    page = window.trash_page
    window.sidebar.trash_button.click()
    assert window.sidebar.trash_button.isChecked() and not window.sidebar.tools_button.isChecked()
    qtbot.waitUntil(lambda: page.model.rowCount() == 2, timeout=10000)
    names = {it.name for it in page.model.items}
    assert names == {"지울사진.png", "b.txt"}
    photo = next(it for it in page.model.items if it.name == "지울사진.png")
    assert "사진" in photo.note
    page.thumbs.get(photo)
    qtbot.waitUntil(lambda: photo.rel in page.thumbs.pixmaps, timeout=10000)

    row = page.model.items.index(photo)
    page._preview(page.model.index(row, 0))
    qtbot.waitUntil(lambda: window.center.currentWidget() is window.viewer, timeout=10000)
    assert window.viewer.memory and not window.viewer.external.isVisibleTo(window.viewer)
    qtbot.waitUntil(lambda: not window.viewer.image.item.pixmap().isNull(), timeout=5000)
    window.go_back()
    assert window.center.currentWidget() is page

    page.grid.selectionModel().select(page.model.index(row, 0), page.grid.selectionModel().SelectionFlag.Select)
    assert page.restore_btn.isEnabled()
    page.restore_btn.click()
    qtbot.waitUntil(lambda: (s.root / "사진/지울사진.png").exists() and page.model.rowCount() == 1, timeout=15000)

    page.grid.selectAll()
    monkeypatch.setattr(trash_mod, "ask", lambda *a, **k: True)
    page.purge_btn.click()
    qtbot.waitUntil(lambda: page.model.rowCount() == 0 and "휴지통이 비어" in page.notice_text.text(), timeout=15000)
    assert not (s.root / "b.txt").exists()


def test_locking_hides_the_trash_and_drops_previews(window, qtbot, monkeypatch):
    s = window.session
    _png(s.root / "사진/x.png")
    _connected_and_synced(window, qtbot, monkeypatch)
    (s.root / "사진/x.png").unlink()
    _sync(window, qtbot)
    page = window.trash_page
    window.show_trash()
    qtbot.waitUntil(lambda: page.model.rowCount() == 1, timeout=10000)
    page.thumbs.get(page.model.items[0])
    qtbot.waitUntil(lambda: bool(page.thumbs.pixmaps), timeout=10000)
    window.lock()
    assert page.model.rowCount() == 0 and not page.thumbs.pixmaps and page.client is None
    assert page.notice_button.isVisibleTo(page) and "잠금을 풀면" in page.notice_text.text()


def test_trash_cards_toggle_like_checkboxes(window, qtbot, monkeypatch):
    s = window.session
    (s.root / "a.txt").write_bytes(b"a")
    (s.root / "b.txt").write_bytes(b"b")
    _connected_and_synced(window, qtbot, monkeypatch)
    (s.root / "a.txt").unlink()
    (s.root / "b.txt").unlink()
    _sync(window, qtbot)
    window.show()
    qtbot.waitExposed(window)
    window.show_trash()
    page = window.trash_page
    qtbot.waitUntil(lambda: page.model.rowCount() == 2, timeout=10000)
    qtbot.wait(100)
    center = page.grid.visualRect(page.model.index(0, 0)).center()
    qtbot.mouseClick(page.grid.viewport(), Qt.LeftButton, pos=center)
    assert len(page.selected()) == 1 and page.restore_btn.text() == "복구 (1)"
    qtbot.mouseClick(page.grid.viewport(), Qt.LeftButton, pos=center)
    assert page.selected() == [] and not page.restore_btn.isEnabled()
    page.all_btn.click()
    assert len(page.selected()) == 2 and page.all_btn.isChecked() and page.all_btn.toolTip() == "모두 해제"
    page.all_btn.click()
    assert page.selected() == []


def test_text_encrypt_and_decrypt_share_one_layout(window, qtbot):
    from secret.ui.dialogs.tools import _TextDecrypt, _TextEncrypt

    s = window.session
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    window.show_tools()
    tabs = window.tools_page.tabs
    enc = next(tabs.widget(i) for i in range(tabs.count()) if isinstance(tabs.widget(i), _TextEncrypt))
    dec = next(tabs.widget(i) for i in range(tabs.count()) if isinstance(tabs.widget(i), _TextDecrypt))

    def shape(w):
        lay = w.layout()
        return [type(lay.itemAt(i).widget() or lay.itemAt(i).layout() or lay.itemAt(i)).__name__ for i in range(lay.count())]

    assert shape(enc) == shape(dec)
    assert enc.input.minimumHeight() == dec.input.minimumHeight() == enc.output.minimumHeight() == dec.output.minimumHeight()
    assert enc.output.font().family() == dec.input.font().family()


def test_trash_list_view_search_and_restore_bar(window, qtbot, monkeypatch):
    s = window.session
    (s.root / "사진").mkdir(exist_ok=True)
    (s.root / "사진/a.txt").write_bytes(b"a")
    (s.root / "b.txt").write_bytes(b"bb")
    _connected_and_synced(window, qtbot, monkeypatch)
    (s.root / "사진/a.txt").unlink()
    (s.root / "b.txt").unlink()
    _sync(window, qtbot)
    window.show_trash()
    page = window.trash_page
    qtbot.waitUntil(lambda: page.model.rowCount() == 2, timeout=10000)

    page.view_toggle.setCurrent(1)
    assert page.stack.currentWidget() is page.list
    row = next(i for i, it in enumerate(page.model.items) if it.name == "a.txt")
    assert page.model.index(row, 3).data() == "사진"
    page.list.selectionModel().select(page.model.index(row, 0), page.list.selectionModel().SelectionFlag.Select
                                      | page.list.selectionModel().SelectionFlag.Rows)
    assert page.model.index(row, 0).data(Qt.CheckStateRole) == Qt.Checked
    assert len(page.selected()) == 1 and page.restore_btn.text() == "복구 (1)" and page.purge_btn.isEnabled()

    page.search.setText("b.t")
    assert [it.name for it in page.model.items] == ["b.txt"] and page.selected() == []
    page.search.setText("없는이름")
    assert "조건에 맞는" in page.notice_text.text() and not page.all_btn.isEnabled()
    page.search.clear()

    seen = []
    page.restore_btn.set_progress = lambda v, f=page.restore_btn.set_progress: (seen.append(v), f(v))
    page.grid.selectAll()
    page.restore_btn.click()
    qtbot.waitUntil(lambda: (s.root / "b.txt").exists() and page.model.rowCount() == 0, timeout=15000)
    assert seen[0] == 0.0 and seen[-1] is None and page.restore_btn.progress is None

    page.header.back.click()
    assert window.center.currentWidget() is window.gallery


def test_file_encrypt_button_fills_while_working(window, qtbot, tmp_path):
    from secret.ui.dialogs.tools import _FileCrypt

    done = []
    window.session.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    tools = window.tools_page
    panel = next(tools.tabs.widget(i) for i in range(tools.tabs.count()) if isinstance(tools.tabs.widget(i), _FileCrypt))
    assert tools.tabs.tabText(tools.tabs.count() - 1) == "비밀번호 변경"
    src = tmp_path / "메모.txt"
    src.write_bytes(b"x" * 1000)
    panel._set(src)
    assert "메모.txt" in panel.drop.name.text() and panel.go.text() == "암호화"
    seen = []
    panel.go.set_progress = lambda v, f=panel.go.set_progress: (seen.append(v), f(v))
    panel.go.click()
    qtbot.waitUntil(lambda: panel.open.isVisibleTo(panel), timeout=10000)
    assert seen[0] == 0.0 and 1.0 in seen and seen[-1] is None and panel.go.text() == "암호화"


def test_duplicates_tab_keeps_one_of_each(window, qtbot, monkeypatch):
    from secret.ui.dialogs import duplicates as dup_mod
    from secret.ui.dialogs.duplicates import DuplicatesPanel

    s = window.session
    for rel in ("사진/원본.png", "백업/복사본.png", "백업/또복사.png"):
        _png(s.root / rel, "#123456")
    os.utime(s.root / "사진/원본.png", ns=(1_000_000_000, 1_000_000_000))
    s.refresh_scan()
    qtbot.waitUntil(lambda: "백업/또복사.png" in s.scan.files)
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    tabs = window.tools_page.tabs
    panel = next(tabs.widget(i) for i in range(tabs.count()) if isinstance(tabs.widget(i), DuplicatesPanel))
    assert tabs.tabText(tabs.indexOf(panel)) == "중복 파일"
    panel.find_btn.click()
    qtbot.waitUntil(lambda: panel.cancel is None and len(panel.groups) == 1, timeout=10000)
    head = panel.groups[0]
    kept = head.child(0)
    assert kept.data(0, dup_mod.REL_ROLE) == "사진/원본.png" and kept.checkState(0) == Qt.Unchecked
    assert panel.delete_btn.text() == "선택한 파일 삭제 (2)"
    assert panel.stack.currentWidget() is panel.view

    panel.view_toggle.setCurrent(0)
    assert panel.stack.currentWidget() is panel.grid
    items = panel.model.items
    assert items[0].state == dup_mod.GROUP and [it.rel for it in items[1:]] == [
        head.child(i).data(0, dup_mod.REL_ROLE) for i in range(head.childCount())]
    assert not panel.model.flags(panel.model.index(0, 0)) & Qt.ItemIsSelectable
    picked = {panel.model.items[i.row()].rel for i in panel.grid.selectionModel().selectedIndexes()}
    assert picked == {"백업/복사본.png", "백업/또복사.png"}
    sm = panel.grid.selectionModel()
    row = next(i for i, it in enumerate(items) if it.rel == "백업/복사본.png")
    sm.select(panel.model.index(row, 0), sm.SelectionFlag.Deselect)
    copy = next(head.child(i) for i in range(head.childCount()) if head.child(i).data(0, dup_mod.REL_ROLE) == "백업/복사본.png")
    assert copy.checkState(0) == Qt.Unchecked and panel.delete_btn.text() == "선택한 파일 삭제 (1)"
    copy.setCheckState(0, Qt.Checked)
    assert len({i.row() for i in sm.selectedIndexes()}) == 2
    panel.view_toggle.setCurrent(1)

    kept.setCheckState(0, Qt.Checked)
    refused = []
    monkeypatch.setattr(dup_mod, "inform", lambda *a, **k: refused.append(1))
    panel.delete_btn.click()
    assert refused and (s.root / "사진/원본.png").exists()

    kept.setCheckState(0, Qt.Unchecked)
    monkeypatch.setattr(dup_mod, "ask", lambda *a, **k: True)
    panel.delete_btn.click()
    assert (s.root / "사진/원본.png").exists()
    assert not (s.root / "백업/복사본.png").exists() and not (s.root / "백업/또복사.png").exists()
    assert panel.groups == [] and not panel.delete_btn.isEnabled()


def test_sidebar_and_path_follow_the_drive_kind(qtbot, qapp, usb, monkeypatch):
    from secret.core import drive

    theme.install(qapp)
    monkeypatch.setattr(unlock.UnlockDialog, "exec", lambda self: unlock.UnlockDialog.Rejected)
    monkeypatch.setattr("secret.ui.session.detect_drive", lambda root: drive.EXTERNAL_HDD)
    w = MainWindow(Session(usb, client_factory=lambda token: FakeDiscord()))
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: bool(w.session.scan.files))
    assert "외장 HDD 연결됨" in w.sidebar.usb_state.text()
    crumbs = [w.gallery.crumbs.itemAt(i).widget() for i in range(w.gallery.crumbs.count())]
    assert crumbs[0].text() == "외장 HDD"
    w.sidebar.update_usb(False)
    assert "외장 HDD 분리됨" in w.sidebar.usb_state.text()


def test_viewer_opens_heic_pdf_music_zip_and_more_text(window, qtbot, tmp_path):
    import wave
    import zipfile

    from tests.test_decode import heic_bytes, pdf_file

    s = window.session
    (s.root / "새형식").mkdir()
    (s.root / "새형식/아이폰.heic").write_bytes(heic_bytes((80, 60)))
    pdf_file(s.root / "새형식/문서.pdf", pages=3)
    with wave.open(str(s.root / "새형식/노래.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000)
    with zipfile.ZipFile(s.root / "새형식/묶음.zip", "w") as z:
        z.writestr("안/a.txt", "a")
        z.writestr("b.txt", "bb")
    (s.root / "새형식/설정.json").write_text('{"a": 1}', encoding="utf-8")
    s.refresh_scan()
    qtbot.waitUntil(lambda: "새형식/설정.json" in s.scan.files)
    v = window.viewer

    window.open_file("새형식/아이폰.heic", [])
    qtbot.waitUntil(lambda: not v.image.item.pixmap().isNull(), timeout=5000)
    assert v.image.item.pixmap().width() == 80

    window.open_file("새형식/문서.pdf", [])
    assert v.body.currentWidget() is v.pdf and v.pdf.doc.pageCount() == 3
    assert "3쪽" in v.footer.text()

    window.open_file("새형식/묶음.zip", [])
    qtbot.waitUntil(lambda: v.archive.model.rowCount() == 2, timeout=5000)
    assert "파일 2개" in v.footer.text()

    window.open_file("새형식/노래.wav", [])
    assert v.kind == "audio" and v.video.screen.currentIndex() == 1 and v.video.title.text() == "노래"
    assert not v.video.full_btn.isVisibleTo(v.video)

    window.open_file("새형식/설정.json", [])
    assert v.kind == "text" and v.text.toPlainText() == '{"a": 1}'
    window.open_file("새형식/묶음.zip", [])
    assert not v.dirty_mark.isVisibleTo(v)
    window.show_gallery()

    g = window.gallery
    g.set_folder("새형식")
    for name in ("아이폰.heic", "문서.pdf"):
        it = next(i for i in g.model.items if i.name == name)
        g.thumbs.get(it)
        qtbot.waitUntil(lambda it=it: g.thumbs.cached(it) is not None, timeout=5000)


def test_memory_preview_opens_pdf_and_zip(window, qtbot, tmp_path):
    import io
    import zipfile

    from secret.ui.trash_page import can_preview
    from tests.test_decode import pdf_file

    assert can_preview("a.pdf") and can_preview("b.zip") and can_preview("c.heic") and not can_preview("d.mp3")
    v = window.viewer
    v.open_memory("문서.pdf", pdf_file(tmp_path / "x.pdf").read_bytes(), ("휴지통", ":trash"))
    assert v.body.currentWidget() is v.pdf and v.pdf.doc.pageCount() == 2 and v.memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "a")
    v.open_memory("묶음.zip", buf.getvalue(), ("휴지통", ":trash"))
    qtbot.waitUntil(lambda: v.archive.model.rowCount() == 1, timeout=5000)
    assert not v.external.isVisibleTo(v)


def test_zip_opens_like_a_folder_and_shows_files_inside(window, qtbot):
    import io
    import wave
    import zipfile

    from PySide6.QtGui import QColor, QImage

    s = window.session
    img = QImage(40, 30, QImage.Format_RGB32)
    img.fill(QColor("#ff0000"))
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    wav = io.BytesIO()
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 4000)
    with zipfile.ZipFile(s.root / "묶음.zip", "w") as z:
        z.writestr("사진/빨강.png", bytes(buf.data()))
        z.writestr("사진/메모.txt", "안녕")
        z.writestr("사진/소리.wav", wav.getvalue())
        z.writestr("읽어보기.txt", "hi")
    s.refresh_scan()
    qtbot.waitUntil(lambda: "묶음.zip" in s.scan.files)
    v = window.viewer
    window.open_file("묶음.zip", [])
    qtbot.waitUntil(lambda: v.archive.model.rowCount() == 2, timeout=5000)
    assert [it.name for it in v.archive.model.items] == ["사진", "읽어보기.txt"]
    assert v.zip_toggle.isVisibleTo(v) and v.archive.stack.currentWidget() is v.archive.grid
    v.zip_toggle.setCurrent(1)
    assert v.archive.stack.currentWidget() is v.archive.list

    v.archive.set_folder("사진")
    crumbs = [c.text() for c in _crumb_buttons(v)]
    assert crumbs[-2:] == ["묶음.zip", "사진"]

    v.archive.open_requested.emit("사진/빨강.png")
    qtbot.waitUntil(lambda: not v.image.item.pixmap().isNull(), timeout=5000)
    assert v.zip_entry == "사진/빨강.png" and not v.zip_toggle.isVisibleTo(v)
    assert sorted(p.name for p in s.root.iterdir()) == sorted(["묶음.zip", "사진", "설치.tmp"])
    v.step(1)
    qtbot.waitUntil(lambda: v.zip_entry is not None and v.body.currentWidget() is not v.image, timeout=5000)

    v.archive.open_requested.emit("사진/메모.txt")
    qtbot.waitUntil(lambda: v.text.toPlainText() == "안녕", timeout=5000)
    assert v.text.isReadOnly() and not v.save_btn.isVisibleTo(v)

    v.archive.open_requested.emit("사진/소리.wav")
    qtbot.waitUntil(lambda: v.body.currentWidget() is v.video, timeout=5000)
    assert v.video.title.text() == "소리"

    window.go_back()
    assert v.zip_entry is None and v.archive.folder == "사진" and not v.text.isReadOnly()
    window.go_back()
    assert v.archive.folder == "" and window.center.currentWidget() is v
    window.go_back()
    assert window.center.currentWidget() is window.gallery


def test_settings_page_saves_and_applies(window, qtbot, usb, monkeypatch):
    from secret.core import prefs
    from secret.core.sync_engine import SyncEngine

    s = window.session
    window.sidebar.settings_button.click()
    page = window.settings_page
    assert window.center.currentWidget() is page and window.sidebar.settings_button.isChecked()

    assert not page.restart_btn.isVisibleTo(page)
    page.language.setCurrentIndex(list(i18n_languages()).index("ja"))
    assert prefs.load(usb)["language"] == "ja" and page.restart_btn.isVisibleTo(page)

    page.view_mode.setCurrentIndex(1)
    page.sort.setCurrentIndex(2)
    page.clipboard.setCurrentIndex(3)
    assert window.clipboard.seconds == 120
    page.blur.setChecked(False)
    assert prefs.load(usb) == {"language": "ja", "view": "list", "sort": 2, "clipboard_seconds": 120, "blur_hidden": False}

    assert not page.backup.isVisibleTo(page) and page.locked_note.isVisibleTo(page)
    done = []
    s.create_async(PW, lambda: done.append(1), pytest.fail)
    qtbot.waitUntil(lambda: bool(done))
    assert page.backup.isVisibleTo(page)
    assert page.keep_versions.value() == 30 and page.keep_trash.value() == 100
    page.keep_versions.set_value(10)
    page.keep_trash.set_value(500)
    assert (s.store.settings.keep_versions, s.store.settings.keep_trash) == (10, 500)
    engine = SyncEngine(s.root, window.fake, s.store)
    assert (engine.keep_versions, engine.keep_trash) == (10, 500)

    window.go_back()
    assert window.center.currentWidget() is window.gallery


def i18n_languages():
    from secret import i18n

    return i18n.LANGUAGES


def test_gallery_starts_with_the_saved_view_and_sort(qtbot, qapp, usb, monkeypatch):
    from secret.core import prefs

    prefs.save(usb, {"view": "list", "sort": 2})
    theme.install(qapp)
    monkeypatch.setattr(unlock.UnlockDialog, "exec", lambda self: unlock.UnlockDialog.Rejected)
    w = MainWindow(Session(usb, client_factory=lambda token: FakeDiscord()))
    qtbot.addWidget(w)
    assert w.gallery.stack.currentIndex() == 1 and w.gallery.sort.currentIndex() == 2
