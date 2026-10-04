import io
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QListView, QStackedWidget, QTreeView, QVBoxLayout, QWidget

from ..core.scanner import kind_of
from ..i18n import tr
from .gallery import ITEM_ROLE, CardDelegate, GalleryModel, GridView, Item, layout_cards
from .memory_thumbs import MemoryThumbs
from .sidebar import human
from .worker import run_async

MAX_OPEN_BYTES = 200 * 1024 * 1024


def list_zip(src) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(src) if isinstance(src, bytes) else src) as z:
        return z.infolist()


def zip_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


@dataclass
class ZipEntry:
    path: str
    is_dir: bool
    info: zipfile.ZipInfo | None = None

    @property
    def name(self) -> str:
        return self.path.rpartition("/")[2]

    @property
    def size(self) -> int:
        return self.info.file_size if self.info and not self.is_dir else 0

    @property
    def locked(self) -> bool:
        return bool(self.info and self.info.flag_bits & 0x1)

    @property
    def mtime_ns(self) -> int:
        if not self.info:
            return 0
        try:
            return int(datetime(*self.info.date_time).timestamp() * 1e9)
        except ValueError:
            return 0


class ZipArchive:
    def __init__(self, src: Path | bytes):
        self.src = src
        self.entries: dict[str, ZipEntry] = {}
        for info in list_zip(src):
            path = zip_name(info).replace("\\", "/").strip("/")
            if not path:
                continue
            self._add_parents(path)
            self.entries[path] = ZipEntry(path, info.is_dir(), info)
        self.files = [e for e in self.entries.values() if not e.is_dir]

    def _add_parents(self, path: str) -> None:
        parts = path.split("/")[:-1]
        for i in range(len(parts)):
            d = "/".join(parts[: i + 1])
            self.entries.setdefault(d, ZipEntry(d, True))

    def children(self, folder: str) -> list[ZipEntry]:
        prefix = f"{folder}/" if folder else ""
        kids = [e for p, e in self.entries.items() if p.startswith(prefix) and "/" not in p[len(prefix):]]
        return sorted(kids, key=lambda e: (not e.is_dir, e.name.lower()))

    def read(self, path: str) -> bytes:
        e = self.entries[path]
        with zipfile.ZipFile(io.BytesIO(self.src) if isinstance(self.src, bytes) else self.src) as z:
            return z.read(e.info)

    @property
    def summary(self) -> str:
        return tr("파일 {n}개 (풀면 {size})", n=len(self.files), size=human(sum(e.size for e in self.files)))


class _ZipModel(GalleryModel):
    HEADERS = (tr("이름"), tr("크기"), tr("수정일"), "")


class ArchivePanel(QWidget):
    failed = Signal(str)
    loaded = Signal(str)
    folder_changed = Signal(str)
    open_requested = Signal(str)

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.archive: ZipArchive | None = None
        self.folder = ""
        self.thumbs = MemoryThumbs()
        self.show_path = False
        self.checkboxes = False
        self.card_size = QSize(230, 300)
        self._token = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.model = _ZipModel()
        self.thumbs.ready.connect(self.model.notify)
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
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setIconSize(QSize(20, 20))
        self.list.setColumnHidden(3, True)
        self.list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 110), (2, 160)):
            self.list.header().setSectionResizeMode(col, QHeaderView.Fixed)
            self.list.header().resizeSection(col, width)
        for view in (self.grid, self.list):
            view.setEditTriggers(QAbstractItemView.NoEditTriggers)
            view.doubleClicked.connect(self._activate)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self.list)
        lay.addWidget(self.stack)

    def set_mode(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 0:
            self._fit()

    def _fit(self) -> None:
        self.card_size = layout_cards(self.grid)
        self.grid.doItemsLayout()

    def load(self, src: Path | bytes) -> None:
        self.clear()
        self._token += 1
        token = self._token

        def done(archive: ZipArchive):
            if token != self._token:
                return
            self.archive = archive
            self.thumbs.reset(lambda path: archive.read(path) if archive.entries[path].size <= MAX_OPEN_BYTES else b"")
            self.set_folder("")
            self.loaded.emit(archive.summary)

        run_async(lambda: ZipArchive(src), done,
                  lambda exc: token == self._token and self.failed.emit(tr("이 압축 파일을 읽을 수 없습니다.")))

    def clear(self) -> None:
        self._token += 1
        self.archive = None
        self.folder = ""
        self.thumbs.reset(None)
        self.model.set_items([])

    def set_folder(self, folder: str) -> None:
        if self.archive is None:
            return
        self.folder = folder
        items = []
        for e in self.archive.children(folder):
            children = len(self.archive.children(e.path)) if e.is_dir else 0
            item = Item(e.path, e.name, e.is_dir, e.size, e.mtime_ns, None if e.is_dir else kind_of(e.name), children=children)
            if e.locked:
                item.note = tr("암호 걸림 · {size}", size=human(e.size))
            items.append(item)
        self.model.set_items(items)
        self.folder_changed.emit(folder)
        if self.stack.currentWidget() is self.grid:
            self._fit()

    def entry(self, path: str) -> ZipEntry | None:
        return self.archive.entries.get(path) if self.archive else None

    def files_here(self) -> list[str]:
        return [it.rel for it in self.model.items if not it.is_dir]

    def _activate(self, index) -> None:
        it: Item = index.data(ITEM_ROLE)
        if it is None:
            return
        if it.is_dir:
            self.set_folder(it.rel)
        else:
            self.open_requested.emit(it.rel)
