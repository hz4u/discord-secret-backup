import threading
from datetime import datetime

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QListView,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.duplicates import find_duplicates
from ...core.scanner import kind_of
from ...i18n import tr
from .. import theme
from ..gallery import GAP, ITEM_ROLE, CardDelegate, GalleryModel, GridView, Item, ThumbCache, in_hidden_folder, layout_cards
from ..messages import friendly
from ..sidebar import human
from ..widgets import ProgressButton, SegmentedToggle, ask, button, inform, label, set_icon
from ..worker import run_async

REL_ROLE = Qt.UserRole + 1
GROUP = "group"
PAD = 24


class _DupModel(GalleryModel):
    def flags(self, index):
        if self.items[index.row()].state == GROUP:
            return Qt.ItemIsEnabled
        return super().flags(index)


class _DupDelegate(CardDelegate):
    TITLE_HEIGHT = 44

    def sizeHint(self, option, index):  # noqa: N802
        it: Item = index.data(ITEM_ROLE)
        if it and it.state == GROUP:
            return QSize(self.gallery.row_width, self.TITLE_HEIGHT)
        return super().sizeHint(option, index)

    def paint(self, painter, option, index) -> None:
        it: Item = index.data(ITEM_ROLE)
        if not (it and it.state == GROUP):
            super().paint(painter, option, index)
            return
        painter.save()
        f = QFont(option.font)
        f.setPixelSize(14)
        f.setWeight(QFont.DemiBold)
        painter.setFont(f)
        painter.setPen(QColor(theme.TEXT))
        r = QRectF(option.rect).adjusted(GAP / 2, 0, -GAP / 2, -6)
        painter.drawText(r, Qt.AlignLeft | Qt.AlignBottom, painter.fontMetrics().elidedText(it.name, Qt.ElideMiddle, int(r.width())))
        painter.restore()


class DuplicatesPanel(QWidget):
    full_width = True

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.cancel: threading.Event | None = None
        self.groups: list[QTreeWidgetItem] = []
        self.thumbs = ThumbCache(session)
        self.show_path = True
        self.checkboxes = True
        self.card_size = QSize(230, 300)
        self.row_width = 800

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 0)
        lay.setSpacing(10)
        intro = label(tr("크기와 내용이 똑같은 파일을 찾습니다. 묶음마다 가장 오래된 하나만 남기고 나머지를 체크해 둡니다. 같은 내용이 하나씩 남으므로 지워도 잃는 내용은 없습니다."), "Muted", wrap=True)
        intro.setContentsMargins(PAD, 0, PAD, 0)
        lay.addWidget(intro)
        top = QHBoxLayout()
        top.setContentsMargins(PAD, 0, PAD, 0)
        self.find_btn = ProgressButton(tr("중복 찾기"))
        self.find_btn.setCursor(Qt.PointingHandCursor)
        self.find_btn.setMinimumWidth(160)
        set_icon(self.find_btn, "copy", theme.ACCENT_INK)
        self.find_btn.clicked.connect(self.find)
        top.addWidget(self.find_btn)
        top.addStretch()
        self.view_toggle = SegmentedToggle(["squares-four", "list-bullets"], [tr("격자로 보기"), tr("목록으로 보기")])
        top.addWidget(self.view_toggle)
        top.addSpacing(12)
        self.delete_btn = button(tr("선택한 파일 삭제"), "Danger", "trash", theme.DANGER)
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self.delete)
        top.addWidget(self.delete_btn)
        lay.addLayout(top)

        self.view = QTreeWidget()
        self.view.setObjectName("List")
        self.view.setHeaderLabels([tr("이름"), tr("위치"), tr("크기"), tr("수정일")])
        self.view.setUniformRowHeights(True)
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 220), (2, 100), (3, 150)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            header.resizeSection(col, width)
        self.view.itemChanged.connect(self._tree_changed)
        self.model = _DupModel()
        self.thumbs.ready.connect(self.model.notify)
        self.grid = GridView()
        self.grid.setObjectName("Grid")
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(False)
        self.grid.setSelectionMode(QAbstractItemView.MultiSelection)
        self.grid.setMouseTracking(True)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.verticalScrollBar().setSingleStep(24)
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(_DupDelegate(self))
        self.grid.resized.connect(self._fit)
        self.grid.selectionModel().selectionChanged.connect(lambda *_: self._grid_changed())
        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.view)
        lay.addWidget(self.stack, 1)
        self.view_toggle.changed.connect(self._show_view)
        self.view_toggle.setCurrent(1)
        self.status = label("", "Muted", wrap=True)
        self.status.setContentsMargins(PAD, 0, PAD, 0)
        lay.addWidget(self.status)
        self._syncing = False
        session.scan_changed.connect(self._scan_changed)
        session.lock_changed.connect(self._on_lock)

    def _show_view(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 0:
            self._fit()

    def _fit(self) -> None:
        self.card_size = layout_cards(self.grid)
        cell = self.card_size.width() + GAP
        self.row_width = max(cell, self.grid.viewport().width() // cell * cell)
        self.grid.doItemsLayout()

    def _rebuild_grid(self) -> None:
        hidden = self.session.hidden_folders
        items = []
        for n, head in enumerate(self.groups):
            items.append(Item(f"#{n}", head.text(0), False, state=GROUP))
            for i in range(head.childCount()):
                child = head.child(i)
                rel = child.data(0, REL_ROLE)
                f = self.session.scan.files.get(rel) or self.session.scan.excluded.get(rel)
                size, mtime = (f.size, f.mtime_ns) if f else (0, 0)
                name = rel.rpartition("/")[2]
                items.append(Item(rel, name, False, size, mtime, kind_of(name), veiled=in_hidden_folder(rel, hidden)))
        self._syncing = True
        self.model.set_items(items)
        self._select_checked()
        self._syncing = False
        if self.stack.currentWidget() is self.grid:
            self._fit()

    def _select_checked(self) -> None:
        picked = {rel for _, rels in self.checked() for rel in rels}
        selection = QItemSelection()
        for row, it in enumerate(self.model.items):
            if it.rel in picked:
                idx = self.model.index(row, 0)
                selection.select(idx, idx)
        self.grid.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect)

    def _tree_changed(self, *_) -> None:
        if not self._syncing:
            self._syncing = True
            self._select_checked()
            self._syncing = False
        self._update()

    def _grid_changed(self) -> None:
        if self._syncing:
            return
        rows = {idx.row() for idx in self.grid.selectionModel().selectedIndexes()}
        picked = {self.model.items[r].rel for r in rows}
        self._syncing = True
        self.view.blockSignals(True)
        for head in self.groups:
            for i in range(head.childCount()):
                child = head.child(i)
                child.setCheckState(0, Qt.Checked if child.data(0, REL_ROLE) in picked else Qt.Unchecked)
        self.view.blockSignals(False)
        self._syncing = False
        self._update()

    def find(self) -> None:
        if self.cancel is not None:
            self.cancel.set()
            self.find_btn.setText(tr("멈추는 중..."))
            return
        s = self.session
        files = {**s.scan.files, **s.scan.excluded}
        root = s.root
        self.cancel = cancel = threading.Event()
        self.view.clear()
        self.groups = []
        self._rebuild_grid()
        self.delete_btn.setEnabled(False)
        self.find_btn.set_progress(0.0)
        self.find_btn.setText(tr("찾는 중... 0%"))
        self.status.setText(tr("같은 크기의 파일만 읽어 비교합니다."))

        def on_progress(p):
            done, total, current = p
            frac = done / total if total else 0.0
            self.find_btn.set_progress(frac)
            self.find_btn.setText(tr("찾는 중... {v1}%", v1=int(frac * 100)))
            self.status.setText(current.rpartition("/")[2])

        def finish():
            self.cancel = None
            self.find_btn.set_progress(None)
            self.find_btn.setText(tr("중복 찾기"))

        def done(result):
            finish()
            self.fill(result, files)

        def failed(exc):
            finish()
            self.status.setText(tr("찾지 못했습니다: {v1}", v1=friendly(exc)))

        run_async(lambda progress: find_duplicates(root, files, cancel, lambda d, t, cur: progress((d, t, cur))),
                  done, failed, on_progress)

    def fill(self, result, files) -> None:
        self.view.blockSignals(True)
        self.view.clear()
        self.groups = []
        bold = QFont(self.view.font())
        bold.setWeight(QFont.DemiBold)
        muted = QColor(theme.MUTED)
        for g in result.groups:
            name = g.files[0].rpartition("/")[2]
            head = QTreeWidgetItem([tr("{name} · 같은 파일 {n}개 · 각 {size}", name=name, n=len(g.files), size=human(g.size))])
            head.setFont(0, bold)
            head.setFlags(Qt.ItemIsEnabled)
            self.view.addTopLevelItem(head)
            head.setFirstColumnSpanned(True)
            for i, rel in enumerate(g.files):
                f = files[rel]
                folder = rel.rpartition("/")[0] or self.session.drive.name
                when = datetime.fromtimestamp(f.mtime_ns / 1e9).strftime("%Y.%m.%d %H:%M")
                child = QTreeWidgetItem([rel.rpartition("/")[2], folder, human(f.size), when])
                child.setData(0, REL_ROLE, rel)
                child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
                child.setCheckState(0, Qt.Unchecked if i == 0 else Qt.Checked)
                child.setIcon(0, theme.icon("file", color=theme.MUTED))
                for col in (1, 2, 3):
                    child.setForeground(col, muted)
                head.addChild(child)
            head.setExpanded(True)
            self.groups.append(head)
        self.view.blockSignals(False)
        self._rebuild_grid()
        if result.cancelled:
            text = tr("중간에 멈췄습니다. 찾은 데까지만 보여 줍니다.")
        elif result.groups:
            text = tr("중복 묶음 {n}개 · 하나씩만 남기면 {size}를 아낄 수 있습니다.", n=len(result.groups), size=human(result.wasted))
        else:
            text = tr("중복 파일이 없습니다.")
        if result.unreadable:
            text += tr(" 읽지 못한 파일 {n}개는 건너뛰었습니다.", n=len(result.unreadable))
        self.status.setText(text)
        self._update()

    def _scan_changed(self) -> None:
        if self.groups and self.cancel is None:
            self.status.setText(tr("폴더가 바뀌었습니다. 결과가 맞지 않을 수 있으니 다시 찾으세요."))

    def _on_lock(self) -> None:
        if not self.session.unlocked:
            if self.cancel is not None:
                self.cancel.set()
            self.view.clear()
            self.groups = []
            self.model.set_items([])
            self.thumbs.clear()
            self.status.setText("")
            self._update()

    def checked(self) -> list[tuple[QTreeWidgetItem, list[str]]]:
        out = []
        for head in self.groups:
            rels = [head.child(i).data(0, REL_ROLE) for i in range(head.childCount())
                    if head.child(i).checkState(0) == Qt.Checked]
            out.append((head, rels))
        return out

    def _update(self) -> None:
        n = sum(len(rels) for _, rels in self.checked())
        self.delete_btn.setText(tr("선택한 파일 삭제 ({n})", n=n) if n else tr("선택한 파일 삭제"))
        self.delete_btn.setEnabled(bool(n) and self.cancel is None)

    def delete(self) -> None:
        picked = self.checked()
        if any(rels and len(rels) == head.childCount() for head, rels in picked):
            inform(self, tr("중복 파일"), tr("묶음마다 적어도 하나는 남겨야 합니다. 남길 파일의 체크를 풀어 주세요."), error=True)
            return
        rels = [r for _, group in picked for r in group]
        if not rels:
            return
        s = self.session
        size = sum(s.scan.files[r].size if r in s.scan.files else s.scan.excluded[r].size
                   for r in rels if r in s.scan.files or r in s.scan.excluded)
        if not ask(self, tr("중복 파일 삭제"), tr("파일 {n}개({size})를 {name}에서 지웁니다.\n같은 내용의 파일이 묶음마다 하나씩 남습니다.", n=len(rels), size=human(size), name=s.drive.name), yes=tr("삭제"), danger=True):
            return
        failed: dict[str, str] = {}
        for rel in rels:
            try:
                (s.root / rel).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                failed[rel] = exc.strerror or str(exc)
        gone = len(rels) - len(failed)
        s.log(tr("중복 파일 {gone}개를 {name}에서 지웠습니다", gone=gone, name=s.drive.name) + (tr(" · 지우지 못함 {n}개", n=len(failed)) if failed else ""),
              "warn" if failed else "info")
        self.view.blockSignals(True)
        for head, group in picked:
            for i in reversed(range(head.childCount())):
                child = head.child(i)
                rel = child.data(0, REL_ROLE)
                if rel in group and rel not in failed:
                    head.removeChild(child)
            if head.childCount() < 2:
                self.view.takeTopLevelItem(self.view.indexOfTopLevelItem(head))
                self.groups.remove(head)
        self.view.blockSignals(False)
        self._rebuild_grid()
        not_deleted = ", ".join(f"{r} ({why})" for r, why in list(failed.items())[:3])
        self.status.setText(tr("✓ {gone}개를 지웠습니다.", gone=gone) + (tr(" 지우지 못한 파일: {not_deleted}", not_deleted=not_deleted) if failed else ""))
        self._update()
        s.refresh_scan()
