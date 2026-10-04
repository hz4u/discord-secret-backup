import base64
import sys
import threading
import time

from PySide6.QtCore import QEvent, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

from ..core.clipboard_guard import ClipboardGuard
from ..core.discord_api import fetch_guild_icon
from ..core.fswatch import FolderWatcher
from ..core.planner import CHANNEL_LIMIT
from ..core.sync_engine import SyncEngine
from ..i18n import tr
from . import theme
from .dialogs.connect import RESTORE, ConnectDialog
from .dialogs.restore import RestoreDialog
from .dialogs.sync_confirm import SyncConfirmDialog
from .dialogs.tools import ToolsPage
from .dialogs.unlock import UnlockDialog
from .gallery import GalleryView
from .messages import counts_text, friendly
from .right_panel import RightPanel, fmt_duration
from .settings_page import SettingsPage
from .sidebar import Sidebar
from .trash_page import TrashPage
from .viewer import ViewerPage
from .widgets import ask, icon_label, label
from .worker import run_async


def _system_clipboard():
    try:
        from ..core import win_clipboard

        return win_clipboard.get_text, win_clipboard.set_text, win_clipboard.clear
    except (ImportError, OSError, AttributeError):
        store = {"v": None}
        return (lambda: store["v"]), (lambda t: store.__setitem__("v", t)), (lambda: store.__setitem__("v", None))


TRASH_CRUMB = ":trash"
WINDOW_SIZE = (1400, 860)
FS_DEBOUNCE_MS = 400
FS_MAX_DELAY = 2.0


class MainWindow(QMainWindow):
    folder_changed_outside = Signal()

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setWindowTitle("Secret")
        self.setWindowIcon(theme.app_icon())
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        self.resize(min(WINDOW_SIZE[0], area.width() - 40) if area else WINDOW_SIZE[0],
                    min(WINDOW_SIZE[1], area.height() - 60) if area else WINDOW_SIZE[1])
        self.setMinimumSize(1100, 700)
        self.clipboard = ClipboardGuard(*_system_clipboard(), seconds=int(session.pref("clipboard_seconds", 30)))
        self.cancel_sync: threading.Event | None = None

        central = QWidget()
        central.setObjectName("Central")
        central.setAttribute(Qt.WA_StyledBackground, True)
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 1, 0, 0)
        row.setSpacing(0)
        self.sidebar = Sidebar(session)
        self.gallery = GalleryView(session)
        self.right = RightPanel(session)
        row.addWidget(self.sidebar)
        self.tools_page = ToolsPage(session, self.clipboard, lambda: self.ensure_unlocked(), self._create_password,
                                    request_sync=lambda: self.start_sync())
        self.viewer = ViewerPage(session)
        self.trash_page = TrashPage(session, lambda: self.ensure_unlocked())
        self.settings_page = SettingsPage(session)
        self.settings_page.restart_requested.connect(self.restart)
        self._viewer_origin = "gallery"
        self.center = QStackedWidget()
        self.center.addWidget(self.gallery)
        self.center.addWidget(self.viewer)
        self.center.addWidget(self.trash_page)
        self.center.addWidget(self.tools_page)
        self.center.addWidget(self.settings_page)
        row.addWidget(self.center, 1)
        row.addWidget(self.right)
        self.setCentralWidget(central)
        self._build_unplugged_overlay()

        self.sidebar.kind_selected.connect(lambda k: self.show_gallery() and self.gallery.set_kind(k))
        self.sidebar.folder_selected.connect(lambda rel: self.show_gallery() and self.gallery.set_folder(rel))
        self.sidebar.tools_clicked.connect(self.show_tools)
        self.sidebar.trash_clicked.connect(self.show_trash)
        self.sidebar.settings_clicked.connect(self.show_settings)
        session.prefs_changed.connect(self._on_pref)
        self.trash_page.preview_requested.connect(self._open_trash_preview)
        self.trash_page.back_requested.connect(self.go_back)
        self.gallery.folder_changed.connect(self.sidebar.select_folder)
        self.gallery.file_opened.connect(self.open_file)
        self.viewer.back_requested.connect(self.go_back)
        self.viewer.folder_requested.connect(self._viewer_crumb)
        self.viewer.closed.connect(lambda: self.center.setCurrentWidget(self.gallery))
        self.right.connect_requested.connect(self.open_connect)
        self.right.disconnect_requested.connect(self.disconnect)
        self.right.unlock_requested.connect(lambda: self.ensure_unlocked())
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.lock)
        QShortcut(QKeySequence("Alt+Left"), self, activated=self.go_back)
        QShortcut(QKeySequence("Alt+Right"), self, activated=self.go_forward)
        QApplication.instance().installEventFilter(self)
        self.right.restore_requested.connect(self.open_restore)
        self.right.sync_requested.connect(self.start_sync)
        self.right.stop_requested.connect(self.stop_sync)
        session.lock_changed.connect(self._on_lock_changed)

        self._restoring = False
        self._fs_pending = False
        self._fs_first = 0.0
        self._fs_timer = QTimer(self)
        self._fs_timer.setSingleShot(True)
        self._fs_timer.timeout.connect(self._rescan_after_change)
        self.folder_changed_outside.connect(self._on_folder_changed)
        self.watcher: FolderWatcher | None = None
        self._start_watch()

        self._usb_checking = False
        self.usb_timer = QTimer(self)
        self.usb_timer.timeout.connect(self._watch_usb)
        self.usb_timer.start(2000)
        self.clip_timer = QTimer(self)
        self.clip_timer.timeout.connect(self.clipboard.tick)
        self.clip_timer.start(1000)

        session.refresh_scan()

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.MouseButtonPress and isinstance(obj, QWidget) and obj.window() is self:
            if event.button() == Qt.BackButton:
                self.go_back()
                return True
            if event.button() == Qt.ForwardButton:
                self.go_forward()
                return True
        return False

    def showEvent(self, e):  # noqa: N802
        theme.dark_titlebar(self)
        super().showEvent(e)

    def _build_unplugged_overlay(self) -> None:
        self.overlay = QFrame(self.centralWidget())
        self.overlay.setObjectName("Overlay")
        lay = QVBoxLayout(self.overlay)
        lay.setAlignment(Qt.AlignCenter)
        box = QHBoxLayout()
        box.addStretch()
        name = self.session.drive.name
        box.addWidget(icon_label(self.session.drive.icon, theme.DANGER, 56))
        box.addStretch()
        lay.addLayout(box)
        title = label(tr("{name}가 분리되었습니다", name=name), "Big")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        sub = label(tr("기억하던 비밀번호를 지우고 잠갔습니다. {name}를 다시 연결하면 이어서 쓸 수 있습니다.", name=name), "Muted")
        sub.setAlignment(Qt.AlignCenter)
        lay.addWidget(sub)
        self.overlay.hide()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self.overlay.setGeometry(self.centralWidget().rect())

    def _watch_usb(self) -> None:
        if self._usb_checking:
            return
        self._usb_checking = True
        root = self.session.root

        def done(present: bool) -> None:
            self._usb_checking = False
            self._apply_usb(present)

        run_async(root.exists, done, lambda exc: setattr(self, "_usb_checking", False))

    def _start_watch(self) -> None:
        self._stop_watch()
        self.watcher = FolderWatcher(self.session.root, self.folder_changed_outside.emit)
        if not self.watcher.start():
            self.watcher = None

    def _stop_watch(self) -> None:
        if self.watcher:
            self.watcher.stop()
            self.watcher = None

    def _on_folder_changed(self) -> None:
        now = time.monotonic()
        if not self._fs_timer.isActive():
            self._fs_first = now
        if now - self._fs_first >= FS_MAX_DELAY:
            self._fs_timer.stop()
            self._rescan_after_change()
        else:
            self._fs_timer.start(FS_DEBOUNCE_MS)

    def _rescan_after_change(self) -> None:
        if self.session.sync_running or self._restoring:
            self._fs_pending = True
            return
        self._fs_pending = False
        self._fs_first = time.monotonic()
        self.session.refresh_scan()

    def _apply_usb(self, present: bool) -> None:
        if present == self.session.usb_present:
            return
        self.session.usb_present = present
        name = self.session.drive.name
        self.session.log(tr("{name}가 분리되어 잠갔습니다", name=name) if not present else tr("{name}가 다시 연결되었습니다", name=name), "warn" if not present else "info")
        if not present:
            if self.cancel_sync:
                self.cancel_sync.set()
            self.viewer.close_file()
            if self.center.currentWidget() is self.viewer:
                self.center.setCurrentWidget(self.gallery)
            self._stop_watch()
            self.session.lock()
            self.clipboard.expire()
            self.gallery.thumbs.clear()
            self.overlay.setGeometry(self.centralWidget().rect())
            self.overlay.show()
            self.overlay.raise_()
        else:
            self.overlay.hide()
            self._start_watch()
            self.session.refresh_scan()
        self.session.usb_changed.emit(present)

    def ensure_unlocked(self, reason: str = "") -> bool:
        if self.session.unlocked:
            return True
        if not self.session.has_config:
            return False
        return UnlockDialog(self, self.session, reason).exec() == UnlockDialog.Accepted

    def lock(self) -> None:
        if self.session.sync_running:
            return
        rel = self.viewer.current
        if rel and self.center.currentWidget() is self.viewer and self._in_hidden_folder(rel):
            if not self._leave_viewer():
                return
            self.center.setCurrentWidget(self.gallery)
        self.clipboard.expire()
        self.session.lock()

    def open_connect(self) -> None:
        if self.session.has_config and not self.ensure_unlocked():
            return
        mode = "attach" if self.session.has_config else "new"
        code = ConnectDialog(self, self.session, mode).exec()
        if code == RESTORE:
            self.open_restore()
        elif self.session.connected:
            self.session.log(tr("디스코드 서버 '{guild_name}'에 연결했습니다", guild_name=self.session.settings.guild_name))

    def disconnect(self) -> None:
        if not ask(self, tr("연결 해제"), tr("이 {name}에서 봇 토큰을 지웁니다.\n디스코드의 백업은 그대로 남고, 다시 연결하면 이어서 쓸 수 있습니다.", name=self.session.drive.name), yes=tr("연결 해제")):
            return
        st = self.session.settings
        st.token = st.guild_id = st.guild_name = st.bot_name = st.guild_icon = st.guild_icon_hash = ""
        self.session.store.save()
        self.session.settings_saved()
        self.session.log(tr("디스코드 연결을 끊었습니다"))

    def open_restore(self) -> None:
        dialog = RestoreDialog(self, self.session)
        self._restoring = True
        try:
            accepted = dialog.exec() == RestoreDialog.Accepted
        finally:
            self._restoring = False
        if self._fs_pending:
            self._rescan_after_change()
        if not accepted or dialog.restored_into is None:
            return
        if dialog.restored_into.resolve() == self.session.root.resolve():
            self.session.refresh_scan()
            if not self.session.unlocked and self.session.has_config:
                self.session.unlock_async(dialog.password_used, lambda: None, lambda exc: None)

    def _in_hidden_folder(self, rel: str) -> bool:
        return any(rel == h or rel.startswith(h + "/") for h in self.session.hidden_folders)

    def _leave_viewer(self, remember: bool = False) -> bool:
        if self.center.currentWidget() is not self.viewer or (self.viewer.current is None and not self.viewer.memory):
            return True
        if not self.viewer.maybe_leave():
            return False
        rel = self.viewer.current
        memory = self.viewer.memory
        self.viewer.close_file()
        if remember and not memory:
            self.gallery.left_viewer(rel)
        return True

    def _viewer_crumb(self, rel: str) -> None:
        if rel == TRASH_CRUMB:
            self.show_trash()
        elif self.show_gallery():
            self.gallery.set_folder(rel)

    def _open_trash_preview(self, name: str, data: bytes) -> None:
        self.viewer.open_memory(name, data, (tr("휴지통"), TRASH_CRUMB))
        self._viewer_origin = "trash"
        self.center.setCurrentWidget(self.viewer)
        self.sidebar.set_page_active("trash")

    def show_trash(self) -> None:
        if not self._leave_viewer():
            return
        self.center.setCurrentWidget(self.trash_page)
        self.sidebar.set_page_active("trash")
        self.trash_page.activate()

    def open_file(self, rel: str, siblings: list) -> None:
        self._viewer_origin = "gallery"
        self.gallery.opened_in_viewer()
        self.viewer.open(rel, siblings)
        self.center.setCurrentWidget(self.viewer)
        self.sidebar.set_tools_active(False)

    def show_tools(self) -> None:
        if not self._leave_viewer():
            return
        self.center.setCurrentWidget(self.tools_page)
        self.sidebar.set_tools_active(True)

    def show_settings(self) -> None:
        if not self._leave_viewer():
            return
        self.center.setCurrentWidget(self.settings_page)
        self.sidebar.set_page_active("settings")

    def _on_pref(self, key: str) -> None:
        if key == "clipboard_seconds":
            self.clipboard.seconds = int(self.session.pref(key, 30))
        elif key == "blur_hidden":
            self.gallery.grid.viewport().update()

    def restart(self) -> None:
        if not self.close():
            return
        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        QProcess.startDetached(sys.executable, args)
        QApplication.instance().quit()

    def show_gallery(self) -> bool:
        if not self._leave_viewer():
            return False
        self.center.setCurrentWidget(self.gallery)
        self.sidebar.set_tools_active(False)
        return True

    def go_back(self) -> None:
        current = self.center.currentWidget()
        if current in (self.tools_page, self.settings_page):
            self.show_gallery()
        elif current is self.viewer and self.viewer.back_inside():
            pass
        elif current is self.viewer and self._viewer_origin == "trash":
            self.show_trash()
        elif current is self.viewer:
            if self._leave_viewer(remember=True):
                self.center.setCurrentWidget(self.gallery)
        elif current is self.trash_page:
            self.show_gallery()
        else:
            self.gallery.go_back()

    def go_forward(self) -> None:
        if self.center.currentWidget() is self.gallery:
            self.gallery.go_forward()

    def _create_password(self) -> None:
        ConnectDialog(self, self.session, "password").exec()

    def start_sync(self) -> None:
        s = self.session
        if not s.has_config:
            self.open_connect()
            return
        if not self.ensure_unlocked():
            return
        if not s.connected:
            self.open_connect()
            if not s.connected:
                return
        s.sync_running = True
        self.cancel_sync = threading.Event()
        self.right.set_checking()
        s.log(tr("바뀐 내용을 확인하는 중"), "busy")
        client = s.client_factory(s.settings.token)
        engine = SyncEngine(s.root, client, s.store, cancel=self.cancel_sync, on_event=s.log)

        def prepare(progress):
            engine.on_progress = progress
            engine.load_manifest()
            if self._update_guild_look(engine.guild, client):
                s.log(tr("서버 이름과 아이콘을 새로 받았습니다"))
            return engine.plan()

        run_async(prepare, lambda plan: self._confirm(engine, client, plan), lambda exc: self._sync_failed(client, exc), self.right.set_progress)

    def _on_lock_changed(self) -> None:
        s = self.session
        if not (s.unlocked and s.connected and s.usb_present):
            return
        token, guild_id = s.settings.token, s.settings.guild_id

        def work():
            client = s.client_factory(token)
            try:
                return self._update_guild_look(client.guild(guild_id), client)
            finally:
                client.close()

        def done(changed):
            if changed:
                s.settings_saved()
                s.log(tr("서버 이름과 아이콘을 새로 받았습니다"))

        run_async(work, done, lambda exc: s.log(tr("서버 정보를 가져오지 못했습니다: {v1}", v1=friendly(exc)), "warn"))

    def _update_guild_look(self, guild: dict, client) -> bool:
        st = self.session.settings
        if st is None:
            return False
        icon_hash = guild.get("icon") or ""
        if guild.get("name", st.guild_name) == st.guild_name and icon_hash == st.guild_icon_hash:
            return False
        st.guild_name = guild.get("name", st.guild_name)
        icon = fetch_guild_icon(client, st.guild_id, icon_hash) if icon_hash else None
        st.guild_icon = base64.b64encode(icon).decode() if icon else ""
        st.guild_icon_hash = icon_hash if icon else ""
        self.session.store.save()
        return True

    def _confirm(self, engine, client, plan) -> None:
        s = self.session
        if self.cancel_sync.is_set():
            s.log(tr("동기화를 멈췄습니다"), "warn")
            self._sync_finished(client, tr("동기화 멈춤"))
            return
        if plan.is_empty:
            s.log(tr("이미 최신 상태라 올릴 것이 없습니다"))
            self._sync_finished(client, tr("최신 버전입니다"))
            return
        s.log(tr("바뀐 내용: ") + counts_text([
            (tr("새로운 파일"), len(plan.new)), (tr("바뀐 파일"), len(plan.changed)), (tr("삭제된 파일"), len(plan.deleted)),
            (tr("이름이 바뀐 파일"), len(plan.renamed)), (tr("이름이 바뀐 폴더"), plan.renamed_folders if plan.remap_channels else 0),
        ], empty=tr("파일 내용은 그대로 (폴더나 수정 시각만 바뀜)")))
        if plan.unseen:
            s.log(tr("읽지 못한 폴더 안의 파일 {n}개는 확인할 수 없어 백업에서 지우지 않고 그대로 둡니다.", n=len(plan.unseen)), "warn")
        if plan.over_limit:
            s.log(tr("디스코드 채널이 {channel_count_after}개가 되어 한도({CHANNEL_LIMIT})를 넘습니다. 폴더 수를 줄이세요.", channel_count_after=plan.channel_count_after, CHANNEL_LIMIT=CHANNEL_LIMIT), "error")
            self._sync_finished(client, tr("동기화 실패"))
            return
        sizes = {rel: f.size for rel, f in engine.scan_result.files.items()}
        if SyncConfirmDialog(self, plan, sizes, s.settings.history, s.drive.name).exec() != SyncConfirmDialog.Accepted:
            s.log(tr("동기화를 취소했습니다"))
            self._sync_finished(client, "")
            return
        self.right.set_running(True)
        s.log(tr("동기화하는 중"), "busy")

        def run(progress):
            engine.on_progress = progress
            return engine.run(plan)

        run_async(run, lambda r: self._sync_done(client, r), lambda exc: self._sync_failed(client, exc), self.right.set_progress)

    def _sync_done(self, client, result) -> None:
        log = self.session.log
        if result.cancelled:
            log(tr("동기화를 멈췄습니다. 다음에 이어서 올립니다."), "warn")
            self._sync_finished(client, tr("동기화 멈춤"))
            return
        if result.aborted:
            log(tr("인터넷 연결 문제로 멈췄습니다. 다음 동기화 때 이어서 올립니다."), "error")
            self._sync_finished(client, tr("동기화 실패"))
            return
        counts = counts_text([(tr("올린 파일"), len(result.uploaded)), (tr("삭제된 파일"), len(result.deleted)),
                              (tr("올리지 못한 파일"), len(result.failed))])
        head = tr("동기화를 마쳤습니다") if not result.failed else tr("동기화를 마쳤지만 일부 파일을 올리지 못했습니다")
        log(f"{head} ({fmt_duration(result.duration)})" + (f" · {counts}" if counts else ""),
            "warn" if result.failed else "info")
        if result.failed:
            log(tr("실패한 파일은 다음 동기화 때 다시 시도합니다. 지울 파일은 안전을 위해 이번에 지우지 않았습니다."), "warn")
        if result.kept_channels:
            log(tr("직접 쓴 글이 있는 채널 {n}개는 지우지 않고 남겨 두었습니다 (이제 Secret이 관리하지 않습니다)", n=len(result.kept_channels)), "warn")
        if result.deferred:
            log(tr("올리는 동안 바뀐 파일 {n}개는 다음 동기화 때 올립니다.", n=len(result.deferred)), "warn")
        self._sync_finished(client, tr("일부 실패") if result.failed else tr("동기화 완료"))

    def _sync_failed(self, client, exc) -> None:
        self.session.log(tr("동기화하지 못했습니다: {v1}", v1=friendly(exc)), "error")
        self._sync_finished(client, tr("동기화 실패"))

    def _sync_finished(self, client, button_text: str) -> None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        self.session.sync_running = False
        self.cancel_sync = None
        if button_text:
            self.right.finish(button_text)
        else:
            self.right.set_running(False)
        self.session.settings_saved()
        self._fs_pending = False
        self.session.refresh_scan()
        self.trash_page.mark_stale()
        if self.center.currentWidget() is self.trash_page:
            self.trash_page.activate()

    def stop_sync(self) -> None:
        if self.cancel_sync:
            self.cancel_sync.set()
            self.right.stopping()

    def closeEvent(self, e):  # noqa: N802
        if self.center.currentWidget() is self.viewer and not self.viewer.maybe_leave():
            e.ignore()
            return
        if self.session.sync_running:
            if not ask(self, tr("종료"), tr("동기화 중입니다. 멈추고 종료할까요?\n다음 동기화 때 이어서 올립니다."), yes=tr("종료"), danger=True):
                e.ignore()
                return
            if self.cancel_sync:
                self.cancel_sync.set()
        self.viewer.close_file()
        self.trash_page.clear()
        self._stop_watch()
        self.clipboard.expire()
        self.gallery.thumbs.clear()
        super().closeEvent(e)
