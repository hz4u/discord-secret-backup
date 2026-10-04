from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

VIDEO_EXTS = {"mp4", "m4v", "mov", "mkv", "avi", "webm", "wmv", "flv", "3gp", "mpg", "mpeg"}
AUDIO_EXTS = {"mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "wma", "aiff", "aif"}
PLAYER_EXTS = VIDEO_EXTS | AUDIO_EXTS
TIMEOUT_MS = 5000
SEEK_MS = 1000


class VideoFrameGrabber(QObject):
    done = Signal(str, QImage)

    def __init__(self, size: int = 480):
        super().__init__()
        self.size = size
        self._queue: deque[tuple[str, Path]] = deque()
        self._busy = False
        self._stop_current = None

    def shutdown(self) -> None:
        self._queue.clear()
        if self._stop_current:
            self._stop_current()

    def request(self, key: str, path: Path) -> None:
        self._queue.append((key, path))
        self._next()

    def _next(self) -> None:
        if self._busy or not self._queue:
            return
        self._busy = True
        key, path = self._queue.popleft()
        audio = path.suffix.lower().lstrip(".") in AUDIO_EXTS
        player = QMediaPlayer(self)
        sink = QVideoSink(self)
        player.setVideoSink(sink)
        timer = QTimer(self)
        timer.setSingleShot(True)
        state = {"finished": False, "seeked": False}

        def finish(img: QImage) -> None:
            if state["finished"]:
                return
            state["finished"] = True
            timer.stop()
            player.stop()
            for obj in (player, sink, timer):
                obj.deleteLater()
            self._busy = False
            self._stop_current = None
            self.done.emit(key, img)
            self._next()

        def on_status(status) -> None:
            if status == QMediaPlayer.LoadedMedia and audio:
                meta = player.metaData()
                cover = meta.value(meta.Key.CoverArtImage) or meta.value(meta.Key.ThumbnailImage)
                img = cover if isinstance(cover, QImage) and not cover.isNull() else QImage()
                if not img.isNull() and max(img.width(), img.height()) > self.size:
                    img = img.scaled(self.size, self.size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                finish(img)
            elif status == QMediaPlayer.LoadedMedia and not state["seeked"]:
                state["seeked"] = True
                duration = player.duration()
                if duration > SEEK_MS * 2:
                    player.setPosition(SEEK_MS)
                player.play()
            elif status in (QMediaPlayer.InvalidMedia, QMediaPlayer.EndOfMedia) and not state["finished"]:
                finish(QImage())

        def on_frame(frame) -> None:
            if not state["seeked"] or not frame.isValid():
                return
            img = frame.toImage()
            if img.isNull():
                return
            if max(img.width(), img.height()) > self.size:
                img = img.scaled(self.size, self.size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            finish(img)

        player.mediaStatusChanged.connect(on_status)
        player.errorOccurred.connect(lambda *_: finish(QImage()))
        sink.videoFrameChanged.connect(on_frame)
        timer.timeout.connect(lambda: finish(QImage()))
        timer.start(TIMEOUT_MS)
        self._stop_current = lambda: finish(QImage())
        player.setSource(QUrl.fromLocalFile(str(path)))
