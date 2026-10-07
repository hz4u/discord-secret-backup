import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPointF, QRect, QRectF, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListView,
    QMenu,
    QProgressDialog,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..core.file_crypto import unique_path
from ..core.names import validate_name
from ..core.scanner import ARCHIVE_EXTS, OTHER
from ..i18n import tr
from . import theme
from .decode import can_thumb, ext_of, read_image
from .filetypes import open_external, viewer_kind
from .nav_header import NavHeader, folder_trail, tool_button
from .sidebar import human
from .video_thumbs import PLAYER_EXTS, VideoFrameGrabber
from .widgets import SegmentedToggle, ask, inform, label
from .worker import run_async

ITEM_ROLE = Qt.UserRole + 1
KIND_ICON = {"image": "image", "video": "film-strip", "document": "file-text", "music": "music-notes", None: "file"}


def file_icon(item) -> str:
    if item.is_dir:
        return "folder-simple"
    if item.kind == OTHER and item.name.rpartition(".")[2].lower() in ARCHIVE_EXTS:
        return "archive"
    return KIND_ICON.get(item.kind, "file")
SORTS = ((tr("최신순"), "mtime", True), (tr("오래된순"), "mtime", False), (tr("이름순"), "name", False), (tr("크기순"), "size", True))
CARD_MIN = 230
GAP = 16
OUTER = 24
WRAP_SLACK = 8
INFO_HEIGHT = 58


def card_height(width: int) -> int:
    return int(width * 0.6) + INFO_HEIGHT


@dataclass
class Item:
    rel: str
    name: str
    is_dir: bool
    size: int = 0
    mtime_ns: int = 0
    kind: str | None = None
    excluded: bool = False
    children: int = 0
    state: str = ""
    hidden: bool = False
    veiled: bool = False
    note: str = ""

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.mtime_ns / 1e9).strftime("%Y.%m.%d %H:%M") if self.mtime_ns else ""


def parent_of(rel: str) -> str:
    return rel.rpartition("/")[0]


class _ThumbSignals(QObject):
    done = Signal(str, QImage)


class _ThumbJob(QRunnable):
    def __init__(self, key: str, src: Path, signals: _ThumbSignals, size: int):
        super().__init__()
        self.key, self.src, self.signals, self.size = key, src, signals, size

    def run(self) -> None:
        try:
            img = read_image(self.src, self.src.name, self.size)
        except ValueError:
            img = QImage()
        self.signals.done.emit(self.key, img)


VEIL_WORK_WIDTH = 160
VEIL_RADIUS = 28


def veil(pix: QPixmap) -> QPixmap:
    if pix.isNull():
        return pix
    w = VEIL_WORK_WIDTH
    h = max(1, round(w * pix.height() / max(1, pix.width())))
    pad = VEIL_RADIUS * 2
    stretched = pix.scaled(w + 2 * pad, h + 2 * pad, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(stretched)
    blur = QGraphicsBlurEffect()
    blur.setBlurRadius(VEIL_RADIUS)
    blur.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(blur)
    scene.addItem(item)
    small = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    small.fill(QColor(theme.PANEL))
    p = QPainter(small)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    scene.render(p, QRectF(0, 0, w, h), QRectF(pad, pad, w, h))
    p.fillRect(small.rect(), QColor(0, 0, 0, 60))
    p.end()
    return QPixmap.fromImage(small).scaled(pix.width(), pix.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def in_hidden_folder(rel: str, hidden: set[str]) -> bool:
    return any(rel.startswith(h + "/") for h in hidden)


class ThumbCache(QObject):
    ready = Signal(str)

    def __init__(self, session, size: int = 480, limit: int = 300):
        super().__init__()
        self.session = session
        self.size = size
        self.limit = limit
        self.pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.signals = _ThumbSignals()
        self.signals.done.connect(self._done)
        self.videos = VideoFrameGrabber(size)
        self.videos.done.connect(self._done)

    @staticmethod
    def _key(item: Item) -> str:
        return f"{item.rel}|{item.size}|{item.mtime_ns}"

    @staticmethod
    def _thumbable(item: Item) -> bool:
        return not item.is_dir and (can_thumb(item.rel) or ext_of(item.rel) in PLAYER_EXTS)

    def cached(self, item: Item) -> QPixmap | None:
        return self.pixmaps.get(self._key(item))

    def get(self, item: Item) -> QPixmap | None:
        if not self._thumbable(item):
            return None
        key = self._key(item)
        pix = self.pixmaps.get(key)
        if pix is not None:
            self.pixmaps.move_to_end(key)
            return pix
        if key not in self._pending and key not in self._failed:
            self._pending.add(key)
            path = self.session.root / item.rel
            if ext_of(item.rel) in PLAYER_EXTS:
                self.videos.request(key, path)
            else:
                self.pool.start(_ThumbJob(key, path, self.signals, self.size))
        return None

    def _done(self, key: str, img: QImage) -> None:
        self._pending.discard(key)
        if img.isNull():
            self._failed.add(key)
            return
        self.pixmaps[key] = QPixmap.fromImage(img)
        while len(self.pixmaps) > self.limit:
            self.pixmaps.popitem(last=False)
        self.ready.emit(key.split("|", 1)[0])

    def veiled(self, item: Item, pix: QPixmap) -> QPixmap:
        key = "veil|" + self._key(item)
        cached = self.pixmaps.get(key)
        if cached is None:
            cached = self.pixmaps[key] = veil(pix)
            while len(self.pixmaps) > self.limit:
                self.pixmaps.popitem(last=False)
        return cached

    def clear(self) -> None:
        self.videos.shutdown()
        self.pixmaps.clear()
        self._failed.clear()
        self._pending.clear()

class GalleryModel(QAbstractTableModel):
    HEADERS = (tr("이름"), tr("크기"), tr("수정일"), tr("백업"))

    def __init__(self):
        super().__init__()
        self.items: list[Item] = []
        self.rows: dict[str, int] = {}

    def set_items(self, items: list[Item]) -> None:
        self.beginResetModel()
        self.items = items
        self.rows = {it.rel: i for i, it in enumerate(items)}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        it = self.items[index.row()]
        col = index.column()
        if role == ITEM_ROLE:
            return it
        if role == Qt.DisplayRole:
            if col == 0:
                return it.name
            if col == 1:
                return tr("항목 {children}개", children=it.children) if it.is_dir else human(it.size)
            if col == 2:
                return "" if it.is_dir else it.date
            if col == 3:
                return {"synced": tr("✓ 백업됨"), "pending": tr("↑ 올릴 예정"), "excluded": tr("— 제외")}.get(it.state, "")
        if role == Qt.DecorationRole and col == 0:
            return theme.icon(file_icon(it),
                              color=theme.TEXT if it.is_dir else theme.MUTED)
        if role == Qt.ForegroundRole:
            if it.excluded:
                return QColor(theme.FAINT)
            if col == 3 and it.state == "synced":
                return QColor(theme.OK)
            if col in (1, 2, 3):
                return QColor(theme.MUTED)
        return None

    def notify(self, rel: str) -> None:
        row = self.rows.get(rel)
        if row is not None:
            self.dataChanged.emit(self.index(row, 0), self.index(row, 0))


class CardDelegate(QStyledItemDelegate):
    def __init__(self, gallery):
        super().__init__(gallery)
        self.gallery = gallery

    def sizeHint(self, option, index):  # noqa: N802
        w = self.gallery.card_size.width()
        return QSize(w + GAP, card_height(w) + GAP)

    def paint(self, painter: QPainter, option, index) -> None:
        it: Item = index.data(ITEM_ROLE)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        half = GAP / 2
        r = QRectF(option.rect).adjusted(half + 0.5, half + 0.5, -half - 0.5, -half - 0.5)
        selected = option.state & QStyle.State_Selected
        hover = option.state & QStyle.State_MouseOver
        painter.setOpacity(0.45 if it.excluded else 1.0)

        painter.setPen(QColor(theme.ACCENT if selected else theme.BORDER_HI if hover else theme.BORDER))
        painter.setBrush(QColor(theme.CARD_HI if hover else theme.CARD))
        painter.drawRoundedRect(r, 10, 10)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(r, 10, 10)

        pad = 6
        thumb_h = int(r.width() * 0.6)
        thumb = QRectF(r.left() + pad, r.top() + pad, r.width() - 2 * pad, thumb_h - pad)
        clip = QPainterPath()
        clip.addRoundedRect(thumb, 6, 6)
        painter.save()
        painter.setClipPath(clip)
        pix = self.gallery.thumbs.get(it)
        if pix is not None and it.veiled and self.gallery.session.pref("blur_hidden", True):
            pix = self.gallery.thumbs.veiled(it, pix)
        if pix is not None and not pix.isNull():
            scaled = pix.scaled(thumb.size().toSize(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            sx = (scaled.width() - thumb.width()) / 2
            sy = 0 if ext_of(it.name) == "pdf" else (scaled.height() - thumb.height()) / 2
            painter.drawPixmap(thumb.toRect(), scaled, QRect(int(sx), int(sy), int(thumb.width()), int(thumb.height())))
        else:
            painter.fillRect(thumb, QColor(theme.PANEL))
            name = file_icon(it)
            color = theme.TEXT if it.is_dir else theme.MUTED
            size = int(min(thumb.width(), thumb.height()) * 0.36)
            ic = theme.icon(name, color=color).pixmap(size, size)
            painter.drawPixmap(int(thumb.center().x() - size / 2), int(thumb.center().y() - size / 2), ic)
        painter.restore()

        x = r.left() + 12
        y = thumb.bottom() + 10
        w = r.width() - 24
        f = QFont(option.font)
        f.setPixelSize(15)
        f.setWeight(QFont.DemiBold)
        painter.setFont(f)
        painter.setPen(QColor(theme.TEXT))
        name = painter.fontMetrics().elidedText(it.name, Qt.ElideMiddle, int(w))
        painter.drawText(QRectF(x, y, w, 20), Qt.AlignLeft | Qt.AlignVCenter, name)
        f.setPixelSize(13)
        f.setWeight(QFont.Normal)
        painter.setFont(f)
        painter.setPen(QColor(theme.MUTED))
        fm = painter.fontMetrics()
        left = tr("항목 {children}개", children=it.children) if it.is_dir else (it.note or human(it.size))
        right = ""
        if not it.is_dir:
            right = (parent_of(it.rel) or self.gallery.session.drive.name) if self.gallery.show_path else it.date
            right = fm.elidedText(right, Qt.ElideMiddle, int(w * 0.6))
        right_w = fm.horizontalAdvance(right) + 10 if right else 0
        line = QRectF(x, y + 22, w, 18)
        painter.drawText(line.adjusted(0, 0, -right_w, 0), Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(left, Qt.ElideRight, int(w - right_w)))
        if right:
            painter.drawText(line, Qt.AlignRight | Qt.AlignVCenter, right)

        if pix is not None and it.kind == "video":
            painter.save()
            c = thumb.center()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawEllipse(c, 20, 20)
            w, h = 14.0, 16.0
            left = c.x() - w / 3
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawPolygon(QPolygonF([QPointF(left, c.y() - h / 2), QPointF(left, c.y() + h / 2), QPointF(left + w, c.y())]))
            painter.restore()
        if getattr(self.gallery, "checkboxes", False):
            painter.save()
            painter.setOpacity(1.0)
            box = QRectF(thumb.left() + 8, thumb.top() + 8, 22, 22)
            painter.setPen(QColor(theme.ACCENT if selected else "#FFFFFF"))
            painter.setBrush(QColor(theme.ACCENT) if selected else QColor(0, 0, 0, 120))
            painter.drawRoundedRect(box, 5, 5)
            if selected:
                painter.drawPixmap(box.adjusted(3, 3, -3, -3).toRect(), theme.icon("check", color=theme.ACCENT_INK).pixmap(16, 16))
            painter.restore()
        badge = {"excluded": (tr("제외"), theme.FAINT), "pending": ("↑", theme.ACCENT)}.get(it.state)
        if it.hidden:
            badge = (tr("숨김"), theme.MUTED)
        if badge:
            painter.setOpacity(1.0)
            text, color = badge
            bw = painter.fontMetrics().horizontalAdvance(text) + 14
            br = QRectF(thumb.right() - bw - 6, thumb.top() + 6, bw, 20)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(14, 20, 32, 210))
            painter.drawRoundedRect(br, 10, 10)
            painter.setPen(QColor(color if it.state != "excluded" else theme.MUTED))
            painter.drawText(br, Qt.AlignCenter, text)
        painter.restore()


class GridView(QListView):
    resized = Signal()

    def __init__(self):
        super().__init__()
        theme.add_overlay_scrollbar(self)

    def set_side_margins(self, left: int, right: int) -> None:
        self.setViewportMargins(left, 0, right, 0)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self.resized.emit()


def layout_cards(grid: GridView) -> QSize:
    total = grid.width()
    span = total - 2 * OUTER
    cols = max(1, (span + GAP) // (CARD_MIN + GAP))
    card_w = max(120, (span + GAP) // cols - GAP)
    cells = cols * (card_w + GAP)
    rest = span + GAP - cells
    left = OUTER - GAP // 2 + rest // 2
    right = max(0, total - left - cells - WRAP_SLACK)
    grid.set_side_margins(left, right)
    grid.setSpacing(0)
    grid.setGridSize(QSize())
    return QSize(card_w, card_height(card_w))


def copy_into(sources: list[Path], dest: Path, progress) -> list[Path]:
    jobs: list[tuple[Path, Path]] = []
    for src in sources:
        target = unique_path(dest, src.name)
        if src.is_dir():
            for p in sorted(src.rglob("*")):
                jobs.append((p, target / p.relative_to(src)))
            jobs.insert(0, (src, target))
        else:
            jobs.append((src, target))
    done = []
    for i, (src, dst) in enumerate(jobs):
        progress((i, len(jobs), src.name))
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            done.append(dst)
    progress((len(jobs), len(jobs), ""))
    return done


class GalleryView(QFrame):
    folder_changed = Signal(str)
    file_opened = Signal(str, list)
    files_changed = Signal()

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setObjectName("Center")
        self.current = ""
        self.kind = None
        self.back_stack: list[str] = []
        self.forward_stack: list[str] = []
        self.pending_file: str | None = None
        self.show_path = False
        self._hidden: set[str] = set()
        self.card_size = QSize(CARD_MIN, 300)
        self.thumbs = ThumbCache(session)
        self.model = GalleryModel()
        self.thumbs.ready.connect(self.model.notify)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.header = NavHeader()
        self.header.back_clicked.connect(self.go_back)
        self.header.forward_clicked.connect(self.go_forward)
        self.header.crumb_clicked.connect(self.set_folder)
        self.back, self.forward, self.crumbs = self.header.back, self.header.forward, self.header.crumbs
        head = self.header.right
        new_folder = tool_button("folder-plus", tr("새 폴더"))
        new_folder.clicked.connect(self.new_folder)
        head.addWidget(new_folder)
        imp = tool_button("download-simple", tr("가져오기 (이 폴더로 복사)"))
        imp.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(imp)
        menu.addAction(theme.icon("file"), tr("파일 가져오기…"), self.import_files)
        menu.addAction(theme.icon("folder-simple"), tr("폴더 가져오기…"), self.import_folder)
        imp.setMenu(menu)
        imp.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        head.addWidget(imp)
        lay.addWidget(self.header)

        tools = QHBoxLayout()
        tools.setContentsMargins(24, 6, 24, 14)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("파일명으로 검색하기..."))
        self.search.addAction(theme.icon("magnifying-glass"), QLineEdit.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.search.setMinimumHeight(40)
        tools.addWidget(self.search, 1)
        tools.addSpacing(12)
        self.sort = QComboBox()
        for text, _, _ in SORTS:
            self.sort.addItem(text)
        self.sort.setMinimumHeight(40)
        self.sort.setFixedWidth(170)
        self.sort.setCursor(Qt.PointingHandCursor)
        self.sort.currentIndexChanged.connect(self.refresh)
        tools.addWidget(self.sort)
        tools.addSpacing(12)
        self.view_toggle = SegmentedToggle(["squares-four", "list-bullets"], [tr("격자로 보기"), tr("목록으로 보기")])
        tools.addWidget(self.view_toggle)
        self.view_toggle.changed.connect(lambda i: self.stack.setCurrentIndex(i))
        lay.addLayout(tools)

        self.grid = GridView()
        self.grid.setObjectName("Grid")
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(False)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setMouseTracking(True)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.verticalScrollBar().setSingleStep(24)
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(CardDelegate(self))
        self.grid.resized.connect(self._fit_cards)

        self.list = QTreeView()
        self.list.setObjectName("List")
        self.list.setModel(self.model)
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setIconSize(QSize(20, 20))
        self.list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 110), (2, 160), (3, 120)):
            self.list.header().setSectionResizeMode(col, QHeaderView.Fixed)
            self.list.header().resizeSection(col, width)

        for view in (self.grid, self.list):
            view.doubleClicked.connect(self._activate)
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self._context_menu(v, pos))
            view.setEditTriggers(QAbstractItemView.NoEditTriggers)

        self.stack = QStackedWidget()
        grid_box = QWidget()
        gl = QVBoxLayout(grid_box)
        gl.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(self.grid)
        list_box = QWidget()
        ll = QVBoxLayout(list_box)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.list)
        self.stack.addWidget(grid_box)
        self.stack.addWidget(list_box)
        lay.addWidget(self.stack, 1)
        self.sort.blockSignals(True)
        self.sort.setCurrentIndex(min(max(int(session.pref("sort", 0)), 0), len(SORTS) - 1))
        self.sort.blockSignals(False)
        if session.pref("view", "grid") == "list":
            self.view_toggle.setCurrent(1)

        self.empty = label("", "Muted")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setParent(self.stack)
        self.empty.hide()

        self.footer = label("", "Footer")
        lay.addWidget(self.footer)

        self.setAcceptDrops(True)
        session.scan_changed.connect(self.refresh)
        session.settings_changed.connect(self.refresh)
        self._render_crumbs()

    def set_folder(self, rel: str, remember: bool = True) -> None:
        if rel == self.current:
            return
        self.pending_file = None
        if remember:
            self.back_stack.append(self.current)
            self.forward_stack.clear()
        self.current = rel
        self._render_crumbs()
        self.refresh()
        self.folder_changed.emit(rel)

    def set_kind(self, kind) -> None:
        self.kind = kind
        self.refresh()

    def go_back(self) -> None:
        if self.back_stack:
            target = self.back_stack.pop()
        elif self.current:
            target = parent_of(self.current)
        else:
            return
        self.forward_stack.append(self.current)
        self.set_folder(target, remember=False)

    def go_forward(self) -> None:
        if self.pending_file:
            rel, self.pending_file = self.pending_file, None
            self.forward.setEnabled(bool(self.forward_stack or self.pending_file))
            self.file_opened.emit(rel, self.viewable_files())
            return
        if not self.forward_stack:
            return
        self.back_stack.append(self.current)
        self.set_folder(self.forward_stack.pop(), remember=False)

    def _render_crumbs(self) -> None:
        self.header.set_trail(folder_trail(self.current, self.session.drive.name))
        self.back.setEnabled(bool(self.current or self.back_stack))
        self.forward.setEnabled(bool(self.forward_stack or self.pending_file))

    def refresh(self) -> None:
        scan = self.session.scan
        self._hidden = self.session.hidden_folders
        if self.current and self.current not in scan.dirs:
            dirs = set(scan.dirs)
            parent = self.current
            while parent and parent not in dirs:
                parent = parent_of(parent)
            self.current = parent
            self.pending_file = None
            self._render_crumbs()
            self.folder_changed.emit(parent)
        query = self.search.text().strip().lower()
        prefix = f"{self.current}/" if self.current else ""
        files = {**scan.files, **scan.excluded}
        self.show_path = bool(query or self.kind)

        items: list[Item] = []
        if self.show_path:
            for rel, f in files.items():
                if not rel.startswith(prefix):
                    continue
                name = rel.rpartition("/")[2]
                if self.kind and f.kind != self.kind:
                    continue
                if query and query not in name.lower():
                    continue
                items.append(self._file_item(f))
        else:
            children: dict[str, int] = {}
            for rel in list(scan.dirs) + list(files):
                parent = parent_of(rel)
                children[parent] = children.get(parent, 0) + 1
            for rel in scan.dirs:
                if parent_of(rel) == self.current:
                    items.append(Item(rel, rel.rpartition("/")[2], True, children=children.get(rel, 0),
                                      hidden=rel in self.session.hidden_folders))
            for rel, f in files.items():
                if parent_of(rel) == self.current:
                    items.append(self._file_item(f))

        _, key, reverse = SORTS[self.sort.currentIndex()]
        dirs = sorted((i for i in items if i.is_dir), key=lambda i: i.name.lower())
        rest = [i for i in items if not i.is_dir]
        rest.sort(key=(lambda i: i.name.lower()) if key == "name" else (lambda i: getattr(i, "mtime_ns" if key == "mtime" else "size")), reverse=reverse)
        self.model.set_items(dirs + rest)

        n_files = len(rest)
        total = sum(i.size for i in rest)
        excluded = sum(1 for i in rest if i.excluded)
        text = tr("총 {n_files}개의 파일 ({size})", n_files=n_files, size=human(total))
        if dirs:
            text = tr("폴더 {n}개 · ", n=len(dirs)) + text
        if excluded:
            text += tr(" · 백업 제외 {excluded}", excluded=excluded)
        self.footer.setText(text)
        self._update_empty()

    def _file_item(self, f) -> Item:
        excluded = f.kind is None
        return Item(
            f.rel, f.rel.rpartition("/")[2], False, f.size, f.mtime_ns, f.kind, excluded,
            state=self.session.file_state(f.rel, f.size, f.mtime_ns, excluded),
            veiled=in_hidden_folder(f.rel, self._hidden),
        )

    def _update_empty(self) -> None:
        if self.model.rowCount():
            self.empty.hide()
            return
        if self.search.text() or self.kind:
            self.empty.setText(tr("조건에 맞는 파일이 없습니다."))
        else:
            self.empty.setText(tr("이 폴더는 비어 있습니다.\n파일을 여기로 끌어다 놓거나 가져오기 버튼을 누르세요."))
        self.empty.setGeometry(self.stack.rect())
        self.empty.show()
        self.empty.raise_()

    def _fit_cards(self) -> None:
        self.card_size = layout_cards(self.grid)
        self.grid.doItemsLayout()
        self.empty.setGeometry(self.stack.rect())

    def _selected(self, view) -> list[Item]:
        return [idx.data(ITEM_ROLE) for idx in view.selectionModel().selectedRows(0)] if view is self.list else [
            idx.data(ITEM_ROLE) for idx in view.selectionModel().selectedIndexes()
        ]

    def viewable_files(self) -> list[str]:
        return [it.rel for it in self.model.items if not it.is_dir and viewer_kind(it.rel)]

    def opened_in_viewer(self) -> None:
        self.forward_stack.clear()
        self.pending_file = None
        self.forward.setEnabled(False)

    def left_viewer(self, rel: str) -> None:
        self.pending_file = rel
        self.forward.setEnabled(True)

    def _activate(self, index) -> None:
        it: Item = index.data(ITEM_ROLE)
        if it.is_dir:
            self.set_folder(it.rel)
        elif viewer_kind(it.rel):
            self.file_opened.emit(it.rel, self.viewable_files())
        else:
            open_external(self.session.root / it.rel)

    def _context_menu(self, view, pos) -> None:
        index = view.indexAt(pos)
        menu = QMenu(self)
        if index.isValid():
            if not view.selectionModel().isSelected(index):
                view.setCurrentIndex(index)
            items = self._selected(view) or [index.data(ITEM_ROLE)]
            it = items[0]
            if len(items) == 1:
                menu.addAction(theme.icon("folder-open" if it.is_dir else "eye" if viewer_kind(it.rel) else "arrow-square-out"),
                               tr("열기"), lambda: self._activate(index))
                if not it.is_dir and viewer_kind(it.rel):
                    menu.addAction(theme.icon("arrow-square-out"), tr("Windows 앱으로 열기"),
                                   lambda: open_external(self.session.root / it.rel))
                if not it.is_dir:
                    menu.addAction(theme.icon("magnifying-glass"), tr("탐색기에서 보기"), lambda: self._reveal(it))
                menu.addAction(theme.icon("pencil-simple"), tr("이름 바꾸기"), lambda: self.rename(it))
                menu.addSeparator()
            delete = menu.addAction(theme.icon("trash", color=theme.DANGER), tr("삭제 ({n}개)", n=len(items)) if len(items) > 1 else tr("삭제"))
            delete.triggered.connect(lambda: self.delete(items))
        else:
            menu.addAction(theme.icon("folder-plus"), tr("새 폴더"), self.new_folder)
            menu.addAction(theme.icon("download-simple"), tr("파일 가져오기…"), self.import_files)
        menu.exec(view.viewport().mapToGlobal(pos))

    def _reveal(self, it: Item) -> None:
        subprocess.Popen(["explorer", "/select,", str(self.session.root / it.rel)])

    def _ask_name(self, title: str, text: str = "") -> str | None:
        while True:
            name, ok = QInputDialog.getText(self, "Secret", f"{title}", QLineEdit.Normal, text)
            if not ok:
                return None
            problem = validate_name(name)
            if problem is None:
                return name.strip()
            inform(self, title, problem, error=True)
            text = name

    def new_folder(self) -> None:
        name = self._ask_name(tr("새 폴더"))
        if not name:
            return
        target = self.session.root / self.current / name if self.current else self.session.root / name
        if target.exists():
            inform(self, tr("새 폴더"), tr("같은 이름이 이미 있습니다."), error=True)
            return
        try:
            target.mkdir()
        except OSError as exc:
            inform(self, tr("새 폴더"), tr("폴더를 만들 수 없습니다. ({strerror})", strerror=exc.strerror), error=True)
            return
        self.session.log(tr("폴더를 만들었습니다: {v1}", v1=target.relative_to(self.session.root).as_posix()))
        self._changed()

    def rename(self, it: Item) -> None:
        name = self._ask_name(tr("이름 바꾸기"), it.name)
        if not name or name == it.name:
            return
        src = self.session.root / it.rel
        dst = src.with_name(name)
        if dst.exists():
            inform(self, tr("이름 바꾸기"), tr("같은 이름이 이미 있습니다."), error=True)
            return
        new_rel = f"{parent_of(it.rel)}/{name}".lstrip("/")
        moved = it.is_dir and self._move_hidden(it.rel, new_rel)
        try:
            src.rename(dst)
        except OSError as exc:
            if moved:
                self._move_hidden(new_rel, it.rel)
            inform(self, tr("이름 바꾸기"), tr("이름을 바꿀 수 없습니다. ({strerror})", strerror=exc.strerror), error=True)
            return
        self.session.log(tr("이름을 바꿨습니다: {name} → {name2}", name=it.name, name2=name))
        self._changed()

    def _move_hidden(self, old: str, new: str | None) -> bool:
        if self.session.store is None:
            return False
        st = self.session.store.settings

        def under(rel: str) -> bool:
            return rel == old or rel.startswith(old + "/")

        affected = [r for r in st.hidden_folders if under(r)]
        if not affected:
            return False
        kept = [r for r in st.hidden_folders if not under(r)]
        moved = [new + r[len(old):] for r in affected] if new is not None else []
        st.hidden_folders = sorted(set(kept) | set(moved))
        self.session.store.save()
        return True

    def delete(self, items: list[Item]) -> None:
        files = sum(1 for i in items if not i.is_dir) + sum(
            1 for i in items if i.is_dir for rel in {**self.session.scan.files, **self.session.scan.excluded} if rel.startswith(i.rel + "/")
        )
        what = items[0].name if len(items) == 1 else tr("{n}개 항목", n=len(items))
        detail = tr("\n\n안에 든 파일 {files}개도 함께 지워집니다.", files=files) if any(i.is_dir for i in items) else ""
        if not ask(self, tr("삭제"), tr("'{what}'을(를) 삭제할까요?\n{name}에는 휴지통이 없습니다. 되돌릴 수 없습니다.{detail}", what=what, name=self.session.drive.name, detail=detail), yes=tr("삭제"), danger=True):
            return
        for it in items:
            path = self.session.root / it.rel
            try:
                shutil.rmtree(path) if it.is_dir else path.unlink()
                self.session.log(tr("{name}에서 지웠습니다: {rel}", name=self.session.drive.name, rel=it.rel))
                if it.is_dir:
                    self._move_hidden(it.rel, None)
            except OSError as exc:
                inform(self, tr("삭제"), tr("'{name}'을(를) 지울 수 없습니다. ({strerror})", name=it.name, strerror=exc.strerror), error=True)
        self._changed()

    def import_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, tr("가져올 파일"))
        if names:
            self.import_paths([Path(n) for n in names])

    def import_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, tr("가져올 폴더"))
        if name:
            self.import_paths([Path(name)])

    def import_paths(self, paths: list[Path]) -> None:
        dest = self.session.root / self.current if self.current else self.session.root
        root = self.session.root.resolve()
        for p in paths:
            if p.resolve() == root or root in p.resolve().parents:
                inform(self, tr("가져오기"), tr("{name} 안의 파일은 가져올 수 없습니다. 이동하려면 탐색기를 쓰세요.", name=self.session.drive.name), error=True)
                return
        dialog = QProgressDialog(tr("복사 준비 중..."), tr("취소"), 0, 0, self)
        dialog.setWindowTitle("Secret")
        dialog.setMinimumDuration(400)
        dialog.setWindowModality(Qt.WindowModal)

        def on_progress(p):
            done, total, name = p
            dialog.setMaximum(total)
            dialog.setValue(done)
            dialog.setLabelText(tr("복사 중: {name}", name=name) if name else tr("마무리 중..."))

        def finished(copied):
            dialog.reset()
            self.session.log(tr("파일 {n}개를 가져왔습니다 → {v1}", n=len(copied), v1=self.current or self.session.drive.name))
            self._changed()

        def failed(exc):
            dialog.reset()
            inform(self, tr("가져오기"), tr("복사하지 못했습니다. ({v1})", v1=getattr(exc, 'strerror', None) or exc), error=True)
            self._changed()

        run_async(lambda progress: copy_into(paths, dest, progress), finished, failed, on_progress)

    def _changed(self) -> None:
        self.session.refresh_scan()
        self.files_changed.emit()

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.grid.setStyleSheet(f"QListView#Grid {{ border: 2px dashed {theme.ACCENT}; border-radius: 12px; }}")

    def dragLeaveEvent(self, e):  # noqa: N802
        self.grid.setStyleSheet("")

    def dropEvent(self, e):  # noqa: N802
        self.grid.setStyleSheet("")
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.import_paths(paths)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self.empty.setGeometry(self.stack.rect())
