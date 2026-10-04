from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

from .decode import can_thumb, read_image
from .gallery import Item, veil

THUMB_MAX_BYTES = 20 * 1024 * 1024


class _Signals(QObject):
    done = Signal(str, QImage)


class _ThumbJob(QRunnable):
    def __init__(self, key: str, load, signals: _Signals, size: int, name: str):
        super().__init__()
        self.key, self.load, self.signals, self.size, self.name = key, load, signals, size, name

    def run(self) -> None:
        img = QImage()
        try:
            img = read_image(self.load(), self.name, self.size)
        except Exception:  # noqa: BLE001
            img = QImage()
        self.signals.done.emit(self.key, img)


class MemoryThumbs(QObject):
    ready = Signal(str)

    def __init__(self, size: int = 480, limit: int = 200):
        super().__init__()
        self.size, self.limit = size, limit
        self.pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.signals = _Signals()
        self.signals.done.connect(self._done)
        self.loader = None
        self._generation = 0

    def reset(self, loader) -> None:
        self._generation += 1
        self.loader = loader
        self.pixmaps.clear()
        self._pending.clear()
        self._failed.clear()

    def get(self, item: Item) -> QPixmap | None:
        if self.loader is None or not can_thumb(item.name) or item.size > THUMB_MAX_BYTES:
            return None
        key = item.rel
        pix = self.pixmaps.get(key)
        if pix is not None:
            self.pixmaps.move_to_end(key)
            return pix
        if key not in self._pending and key not in self._failed:
            self._pending.add(key)
            generation, load = self._generation, self.loader
            self.pool.start(_ThumbJob(f"{generation}|{key}", lambda: load(key), self.signals, self.size, item.name))
        return None

    def veiled(self, item: Item, pix: QPixmap) -> QPixmap:
        key = "veil|" + item.rel
        cached = self.pixmaps.get(key)
        if cached is None:
            cached = self.pixmaps[key] = veil(pix)
        return cached

    def _done(self, tagged: str, img: QImage) -> None:
        generation, key = tagged.split("|", 1)
        if int(generation) != self._generation:
            return
        self._pending.discard(key)
        if img.isNull():
            self._failed.add(key)
            return
        self.pixmaps[key] = QPixmap.fromImage(img)
        while len(self.pixmaps) > self.limit:
            self.pixmaps.popitem(last=False)
        self.ready.emit(key)
