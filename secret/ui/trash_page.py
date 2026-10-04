import threading
from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListView,
    QMenu,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.history import KEEP_TRASH
from ..core.restore import MEMORY_BUDGET, MemoryBudget, _UrlBook, read_entry
from ..core.scanner import kind_of
from ..core.sync_engine import SyncEngine
from ..core.timeline import fetch, restored_state
from ..i18n import tr
from . import theme
from .filetypes import MEMORY_KINDS, viewer_kind
from .gallery import ITEM_ROLE, SORTS, CardDelegate, GalleryModel, GridView, Item, in_hidden_folder, layout_cards
from .memory_thumbs import MemoryThumbs
from .messages import friendly
from .nav_header import NavHeader, tool_button
from .sidebar import human
from .widgets import ProgressButton, SegmentedToggle, ask, button, label, set_icon
from .worker import run_async

PREVIEW_MAX_BYTES = 200 * 1024 * 1024


def can_preview(name: str) -> bool:
    return viewer_kind(name) in MEMORY_KINDS


class TrashModel(GalleryModel):
    HEADERS = (tr("이름"), tr("크기"), tr("지운 날짜"), tr("원래 위치"))

    def __init__(self):
        super().__init__()
        self.folders: dict[str, str] = {}
        self.checked: set[int] = set()

    def data(self, index, role=Qt.DisplayRole):
        col = index.column()
        if col == 0 and role == Qt.CheckStateRole:
            return Qt.Checked if index.row() in self.checked else Qt.Unchecked
        if col == 3 and role == Qt.DisplayRole:
            return self.folders.get(self.items[index.row()].rel, "")
        if col == 3 and role == Qt.ForegroundRole:
            return QColor(theme.MUTED)
        return super().data(index, role)

    def set_checked(self, rows: set[int]) -> None:
        if rows != self.checked:
            self.checked = rows
            if self.items:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, 0), [Qt.CheckStateRole])


class TrashPage(QFrame):
    preview_requested = Signal(str, bytes)
    back_requested = Signal()

    def __init__(self, session, request_unlock):
        super().__init__()
        self.setObjectName("Center")
        self.session = session
        self.request_unlock = request_unlock
        self.engine: SyncEngine | None = None
        self.client = None
        self.items: dict[str, object] = {}
        self.all_items: list[Item] = []
        self._load_bytes = None
        self.last_message = ""
        self.stale = True
        self.restoring = False
        self.card_size = QSize(230, 300)
        self.show_path = False
        self.checkboxes = True
        self.thumbs = MemoryThumbs()
        self.model = TrashModel()
        self.thumbs.ready.connect(self.model.notify)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = NavHeader("trash")
        self.header.set_trail([(tr("휴지통"), "")])
        self.header.back.setToolTip(tr("갤러리로 (Alt+←)"))
        self.header.back_clicked.connect(self.back_requested)
        self.header.forward.setEnabled(False)
        self.restore_btn = ProgressButton(tr("복구"))
        self.restore_btn.setCursor(Qt.PointingHandCursor)
        self.restore_btn.setMinimumWidth(130)
        set_icon(self.restore_btn, "arrow-counter-clockwise", theme.ACCENT_INK)
        self.restore_btn.clicked.connect(self.restore)
        self.all_btn = tool_button(theme.svg_icon("select-multiple"), tr("모두 선택"))
        self.all_btn.setCheckable(True)
        self.all_btn.clicked.connect(self._toggle_all)
        self.reload_btn = tool_button("arrows-clockwise-bold", tr("새로고침"))
        self.reload_btn.clicked.connect(self.load)
        self.purge_btn = tool_button("trash", tr("영구 삭제"), theme.DANGER)
        self.purge_btn.clicked.connect(self.purge)
        for b in (self.all_btn, self.reload_btn, self.purge_btn):
            self.header.right.addWidget(b)
        self.header.right.addSpacing(6)
        self.header.right.addWidget(self.restore_btn)
        lay.addWidget(self.header)

        tools = QHBoxLayout()
        tools.setContentsMargins(24, 6, 24, 14)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("파일명으로 검색하기..."))
        self.search.addAction(theme.icon("magnifying-glass"), QLineEdit.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply)
        self.search.setMinimumHeight(40)
        tools.addWidget(self.search, 1)
        tools.addSpacing(12)
        self.sort = QComboBox()
        for text, _, _ in SORTS:
            self.sort.addItem(text)
        self.sort.setMinimumHeight(40)
        self.sort.setFixedWidth(170)
        self.sort.setCursor(Qt.PointingHandCursor)
        self.sort.currentIndexChanged.connect(self._apply)
        tools.addWidget(self.sort)
        tools.addSpacing(12)
        self.view_toggle = SegmentedToggle(["squares-four", "list-bullets"], [tr("격자로 보기"), tr("목록으로 보기")])
        self.view_toggle.changed.connect(lambda _: self._show_items())
        tools.addWidget(self.view_toggle)
        lay.addLayout(tools)

        self.stack = QStackedWidget()
        notice = QWidget()
        nl = QVBoxLayout(notice)
        nl.setAlignment(Qt.AlignCenter)
        self.notice_text = label("", "Muted")
        self.notice_text.setAlignment(Qt.AlignCenter)
        nl.addWidget(self.notice_text)
        self.notice_button = button(tr("잠금 해제"), "Primary")
        self.notice_button.setFixedWidth(200)
        self.notice_button.clicked.connect(lambda: self.request_unlock())
        nl.addWidget(self.notice_button, 0, Qt.AlignCenter)
        self.notice = notice
        self.stack.addWidget(notice)
        self.grid = GridView()
        self.grid.setObjectName("Grid")
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(False)
        self.grid.setMouseTracking(True)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.verticalScrollBar().setSingleStep(24)
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(CardDelegate(self))
        self.grid.resized.connect(self._fit)
        self.list = QTreeView()
        self.list.setObjectName("List")
        self.list.setModel(self.model)
        self.list.setSelectionModel(self.grid.selectionModel())
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setIconSize(QSize(20, 20))
        self.list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 110), (2, 160), (3, 200)):
            self.list.header().setSectionResizeMode(col, QHeaderView.Fixed)
            self.list.header().resizeSection(col, width)
        for view in (self.grid, self.list):
            view.setSelectionMode(QAbstractItemView.MultiSelection)
            view.setSelectionBehavior(QAbstractItemView.SelectRows)
            view.setEditTriggers(QAbstractItemView.NoEditTriggers)
            view.doubleClicked.connect(self._preview)
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self._menu(v, pos))
            self.stack.addWidget(view)
        self.grid.selectionModel().selectionChanged.connect(lambda *_: self._selection_changed())
        lay.addWidget(self.stack, 1)

        self.footer = label("", "Footer")
        lay.addWidget(self.footer)

        session.lock_changed.connect(self._on_lock)
        session.settings_changed.connect(self._update)
        session.settings_changed.connect(self.mark_stale)
        self._update()

    def _keep(self) -> int:
        store = self.session.store
        return (store.settings.keep_trash if store else None) or KEEP_TRASH

    def _fit(self) -> None:
        self.card_size = layout_cards(self.grid)
        self.grid.doItemsLayout()

    def _notice(self, text: str, unlock: bool = False) -> None:
        self.notice_text.setText(text)
        self.notice_button.setVisible(unlock)
        self.stack.setCurrentWidget(self.notice)

    def _listing(self) -> bool:
        return self.stack.currentWidget() is not self.notice

    def _show_items(self) -> None:
        if self.engine is None:
            return
        if not self.all_items:
            self._notice(tr("휴지통이 비어 있습니다.\n동기화할 때 지운 파일이 최근 {n}개까지 여기에 남습니다.", n=self._keep()))
        elif not self.model.rowCount():
            self._notice(tr("조건에 맞는 파일이 없습니다."))
        else:
            self.stack.setCurrentWidget(self.list if self.view_toggle.current == 1 else self.grid)
            if self.stack.currentWidget() is self.grid:
                self._fit()
        self._update()

    def _selection_changed(self) -> None:
        self.model.set_checked({idx.row() for idx in self.grid.selectionModel().selectedIndexes()})
        self._update()

    def _update(self) -> None:
        s = self.session
        busy = s.sync_running
        n = len(self.selected())
        self.restore_btn.setEnabled(bool(n) and not busy)
        if not self.restoring:
            self.restore_btn.setText(tr("복구 ({n})", n=n) if n else tr("복구"))
        self.purge_btn.setEnabled(bool(n) and not busy)
        self.purge_btn.setToolTip(tr("영구 삭제 ({n})", n=n) if n else tr("영구 삭제"))
        listed = self._listing() and self.model.rowCount() > 0
        self.all_btn.setEnabled(listed and not busy)
        every = listed and n == self.model.rowCount()
        self.all_btn.setChecked(every)
        self.all_btn.setToolTip(tr("모두 해제") if every else tr("모두 선택"))
        self.reload_btn.setEnabled(s.unlocked and s.connected and not busy)

    def activate(self) -> None:
        s = self.session
        if not s.unlocked:
            self._notice(tr("잠금을 풀면 휴지통을 볼 수 있습니다.") if s.has_config else tr("메인 비밀번호를 만들고 디스코드에 연결하면 쓸 수 있습니다."),
                         unlock=s.has_config)
        elif not s.connected:
            self._notice(tr("디스코드에 연결하면 휴지통을 볼 수 있습니다."))
        elif self.stale or self.engine is None:
            self.load()
        self._update()

    def mark_stale(self) -> None:
        self.stale = True

    def _on_lock(self) -> None:
        if not self.session.unlocked:
            self.clear()
            self._notice(tr("잠금을 풀면 휴지통을 볼 수 있습니다."), unlock=True)

    def clear(self) -> None:
        self.thumbs.reset(None)
        self.all_items = []
        self.model.set_items([])
        self.model.set_checked(set())
        self.items.clear()
        self.engine = None
        self.stale = True
        if self.client is not None:
            self.client.close()
            self.client = None
        self.footer.setText("")

    def _open_engine(self):
        s = self.session
        client = s.client_factory(s.settings.token)
        return client, SyncEngine(s.root, client, s.store)

    def load(self) -> None:
        s = self.session
        if not (s.unlocked and s.connected):
            self.activate()
            return
        self._notice(tr("디스코드에서 휴지통을 읽는 중..."))
        self.reload_btn.setEnabled(False)

        def work():
            client, engine = self._open_engine()
            try:
                engine.load_manifest()
            except Exception:
                client.close()
                raise
            return client, engine

        def done(out):
            self.clear()
            self.client, self.engine = out
            self.stale = False
            self.fill()
            self._update()

        def failed(exc):
            self._notice(tr("휴지통을 읽지 못했습니다: {v1}", v1=friendly(exc)))
            self._update()

        run_async(work, done, failed)

    def fill(self) -> None:
        m = self.engine.manifest
        hidden = self.session.hidden_folders
        trash = sorted(m.trash, key=lambda t: (t.at, t.rel), reverse=True)
        self.items = {t.entry.message_id: t for t in trash}
        items = []
        folders = {}
        for t in trash:
            name = t.rel.rpartition("/")[2]
            folder = t.rel.rpartition("/")[0] or self.session.drive.name
            folders[t.entry.message_id] = folder
            try:
                when = int(datetime.fromisoformat(t.at).timestamp() * 1e9)
            except ValueError:
                when = 0
            items.append(Item(t.entry.message_id, name, False, t.entry.size, when, kind_of(name),
                              note=f"{folder} · {human(t.entry.size)}", veiled=in_hidden_folder(t.rel, hidden)))
        self.all_items = items
        self.model.folders = folders
        client, dk = self.client, self.session.settings.dk
        book = _UrlBook(client)
        budget = MemoryBudget(MEMORY_BUDGET)
        entries = {mid: t.entry for mid, t in self.items.items()}

        def load(mid: str) -> bytes:
            return read_entry(book, budget, dk, entries[mid], threading.Event())

        self._load_bytes = load
        self.thumbs.reset(load)
        total = sum(t.entry.size for t in trash)
        summary = tr("휴지통 {n}개 ({size}) · 최근 {keep}개까지 보관", n=len(trash), size=human(total), keep=self._keep()) if trash else ""
        self.footer.setText(" · ".join(x for x in (self.last_message, summary) if x))
        self.last_message = ""
        self._apply()

    def _apply(self) -> None:
        query = self.search.text().strip().lower()
        shown = [it for it in self.all_items if query in it.name.lower()] if query else list(self.all_items)
        _, key, reverse = SORTS[self.sort.currentIndex()]
        by = (lambda i: i.name.lower()) if key == "name" else (lambda i: getattr(i, "mtime_ns" if key == "mtime" else "size"))
        shown.sort(key=by, reverse=reverse)
        self.model.set_items(shown)
        self.model.set_checked(set())
        self._show_items()

    def _toggle_all(self) -> None:
        if len(self.selected()) == self.model.rowCount():
            self.grid.clearSelection()
        else:
            self.grid.selectAll()
        self._update()

    def selected(self) -> list:
        if not self._listing():
            return []
        rows = sorted({idx.row() for idx in self.grid.selectionModel().selectedIndexes()})
        items = (self.model.index(r, 0).data(ITEM_ROLE) for r in rows)
        return [self.items[it.rel] for it in items if it and it.rel in self.items]

    def _menu(self, view, pos) -> None:
        index = view.indexAt(pos)
        if not index.isValid():
            return
        sm = view.selectionModel()
        if not sm.isSelected(index):
            sm.select(index, sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
        menu = QMenu(self)
        menu.addAction(theme.icon("arrow-counter-clockwise"), tr("복구"), self.restore)
        menu.addAction(theme.icon("trash", color=theme.DANGER), tr("영구 삭제"), self.purge)
        menu.exec(view.viewport().mapToGlobal(pos))

    def _preview(self, index) -> None:
        it: Item = index.data(ITEM_ROLE)
        if it is None or not can_preview(it.name) or self._load_bytes is None:
            return
        if it.size > PREVIEW_MAX_BYTES:
            self.footer.setText(tr("{name}은(는) 너무 커서 미리 볼 수 없습니다. 복구한 뒤 여세요.", name=it.name))
            return
        load = self._load_bytes
        self.footer.setText(tr("{name} 받는 중...", name=it.name))
        run_async(lambda: load(it.rel), lambda data: self.preview_requested.emit(it.name, data),
                  lambda exc: self.footer.setText(tr("열지 못했습니다: {v1}", v1=friendly(exc))))

    def _begin(self) -> None:
        self.session.sync_running = True
        self._update()

    def _end(self) -> None:
        self.session.sync_running = False
        self.session.settings_saved()
        self.session.refresh_scan()
        self._update()

    def restore(self) -> None:
        chosen = self.selected()
        if not chosen:
            return
        s = self.session
        dk, root = s.settings.dk, s.root
        self._begin()
        self.restoring = True
        self.restore_btn.set_progress(0.0)
        self.restore_btn.setText(tr("복구 중... 0%"))
        self.footer.setText(tr("{n}개를 받는 중...", n=len(chosen)))

        def work(progress):
            client, engine = self._open_engine()
            try:
                engine.load_manifest()
                live = {t.entry.message_id: t for t in engine.manifest.trash}
                picked = [live[t.entry.message_id] for t in chosen if t.entry.message_id in live]
                got = fetch(client, dk, root, {t.rel: t.entry for t in picked}, overwrite=False, on_progress=progress)
                restored = {got.saved_as[t.rel]: t.entry for t in picked if t.rel in got.saved_as}
                if restored:
                    engine.commit_state(restored_state(engine.manifest, restored), kind="trash-restore",
                                        untrash={e.message_id for e in restored.values()})
                return got, restored
            finally:
                client.close()

        def on_progress(p) -> None:
            frac = p.done_bytes / p.total_bytes if p.total_bytes else p.done_files / max(1, p.total_files)
            self.restore_btn.set_progress(frac)
            self.restore_btn.setText(tr("복구 중... {v1}%", v1=int(frac * 100)))
            self.footer.setText(tr("받는 중 {done_files}/{total_files}개", done_files=p.done_files, total_files=p.total_files))

        def finish() -> None:
            self.restoring = False
            self.restore_btn.set_progress(None)
            self._end()

        def done(out):
            got, restored = out
            finish()
            renamed = sum(1 for rel, saved in got.saved_as.items() if rel != saved)
            problems = len(got.failed) + len(got.corrupt)
            self.load()
            text = tr("✓ {n}개를 복구했습니다.", n=len(restored))
            if renamed:
                text += tr(" 같은 이름이 있어 {renamed}개는 '(1)'을 붙여 저장했습니다.", renamed=renamed)
            if problems:
                text += tr(" {problems}개는 받지 못했습니다. 다시 복구해 보세요.", problems=problems)
            self.last_message = text
            s.log(text.removeprefix("✓ "), "info" if not problems else "warn")

        def failed(exc):
            finish()
            self.footer.setText(tr("복구하지 못했습니다: {v1}", v1=friendly(exc)))

        run_async(work, done, failed, on_progress)

    def purge(self) -> None:
        chosen = self.selected()
        if not chosen:
            return
        if not ask(self, tr("영구 삭제"), tr("{n}개를 휴지통에서 뺍니다.\n지우기 전 버전이 기록에 남아 있으면, 그 버전이 정리될 때 디스코드에서도 지워집니다.", n=len(chosen)),
                   yes=tr("영구 삭제"), danger=True):
            return
        s = self.session
        self._begin()

        def work():
            client, engine = self._open_engine()
            try:
                engine.load_manifest()
                engine.commit_state(restored_state(engine.manifest, {}), kind="trash-purge",
                                    untrash={t.entry.message_id for t in chosen})
            finally:
                client.close()

        def done(_):
            self._end()
            self.last_message = tr("✓ {n}개를 휴지통에서 뺐습니다.", n=len(chosen))
            s.log(self.last_message.removeprefix("✓ "))
            self.load()

        def failed(exc):
            self._end()
            self.footer.setText(tr("영구 삭제하지 못했습니다: {v1}", v1=friendly(exc)))

        run_async(work, done, failed)
