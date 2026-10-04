from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMargins, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QMovie, QPainter, QPalette, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import textfile
from ..core.textfile import ChangedOnDisk, NotText, TooLarge
from ..i18n import tr
from . import theme
from .decode import QT_IMAGE_EXTS, open_pdf, read_image
from .filetypes import MEMORY_KINDS, ext_of, open_external, viewer_kind  # noqa: F401
from .nav_header import NavHeader, folder_trail, tool_button
from .sidebar import human
from .widgets import SegmentedToggle, ask, ask_save, button, icon_label, inform, label
from .worker import run_async
from .zip_view import MAX_OPEN_BYTES, ArchivePanel, list_zip, zip_name  # noqa: F401

ZIP_CRUMB = ":zip:"
SEEK_STEP_MS = 5000
ZOOM_STEP = 1.25
ZOOM_RANGE = (0.02, 32.0)


def fmt_ms(ms: int) -> str:
    s = max(0, ms) // 1000
    h, rest = divmod(s, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


class ImageView(QGraphicsView):
    failed = Signal(str)
    loaded = Signal(int, int)
    step_requested = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("ImageView")
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.item)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QColor(theme.BG))
        self.setFocusPolicy(Qt.StrongFocus)
        self.fit = True
        self.movie: QMovie | None = None
        self._token = 0

    def load(self, path: Path) -> None:
        self.clear()
        token = self._token
        movie = QMovie(str(path)) if ext_of(path.name) in QT_IMAGE_EXTS else None
        if movie is not None and movie.isValid() and movie.frameCount() > 1:
            self.movie = movie
            movie.frameChanged.connect(lambda _: self._set_pixmap(movie.currentPixmap(), keep_view=True))
            movie.start()
            size = movie.currentPixmap().size()
            self.loaded.emit(size.width(), size.height())
            return

        def done(img):
            if token != self._token:
                return
            self._set_pixmap(QPixmap.fromImage(img))
            self.loaded.emit(img.width(), img.height())

        run_async(lambda: read_image(path, path.name), done,
                  lambda exc: token == self._token and self.failed.emit(tr("이 사진을 열 수 없습니다.")))

    def load_bytes(self, data: bytes, name: str = "") -> None:
        self.clear()
        token = self._token

        def done(img):
            if token != self._token:
                return
            self._set_pixmap(QPixmap.fromImage(img))
            self.loaded.emit(img.width(), img.height())

        run_async(lambda: read_image(data, name or "image.png"), done,
                  lambda exc: token == self._token and self.failed.emit(tr("이 사진을 열 수 없습니다.")))

    def _set_pixmap(self, pixmap: QPixmap, keep_view: bool = False) -> None:
        self.item.setPixmap(pixmap)
        self.scene().setSceneRect(QRectF(pixmap.rect()))
        if not keep_view:
            self.fit = True
            self.apply_fit()

    def clear(self) -> None:
        self._token += 1
        if self.movie:
            self.movie.stop()
            self.movie.deleteLater()
            self.movie = None
        self.item.setPixmap(QPixmap())

    def apply_fit(self) -> None:
        self.resetTransform()
        rect = self.item.boundingRect()
        if rect.isEmpty():
            return
        view = self.viewport().rect()
        if rect.width() > view.width() or rect.height() > view.height():
            self.fitInView(self.item, Qt.KeepAspectRatio)
        self.centerOn(self.item)

    def zoom(self, factor: float) -> None:
        current = self.transform().m11()
        factor = max(ZOOM_RANGE[0] / current, min(ZOOM_RANGE[1] / current, factor))
        self.fit = False
        self.scale(factor, factor)

    def wheelEvent(self, e):  # noqa: N802
        steps = e.angleDelta().y() / 120
        if steps:
            self.zoom(ZOOM_STEP ** steps)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if self.fit:
            self.fit = False
            self.resetTransform()
            self.centerOn(self.mapToScene(e.position().toPoint()))
        else:
            self.fit = True
            self.apply_fit()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.fit:
            self.apply_fit()

    def keyPressEvent(self, e):  # noqa: N802
        key = e.key()
        if key in (Qt.Key_Left, Qt.Key_Right) and not e.modifiers():
            self.step_requested.emit(-1 if key == Qt.Key_Left else 1)
        elif key in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom(ZOOM_STEP)
        elif key == Qt.Key_Minus:
            self.zoom(1 / ZOOM_STEP)
        elif key == Qt.Key_0:
            self.fit = True
            self.apply_fit()
        else:
            super().keyPressEvent(e)


class ClickSlider(QSlider):
    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            style = self.style()
            groove = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
            handle = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            pos = e.position().toPoint()
            if not handle.contains(pos):
                span = max(1, groove.width() - handle.width())
                x = pos.x() - groove.x() - handle.width() // 2
                value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span)
                self.setValue(value)
                self.sliderMoved.emit(value)
        super().mousePressEvent(e)


class _VideoSurface(QVideoWidget):
    key = Signal(object)
    double_clicked = Signal()

    def keyPressEvent(self, e):  # noqa: N802
        self.key.emit(e)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        self.double_clicked.emit()


class VideoView(QWidget):
    failed = Signal(str)
    loaded = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.audio_only = False
        self._buf = None
        self.surface = _VideoSurface()
        self.surface.setStyleSheet(f"background: {theme.BG};")
        self.surface.key.connect(self.handle_key)
        self.surface.double_clicked.connect(self.toggle_fullscreen)
        self.screen = QStackedWidget()
        self.screen.addWidget(self.surface)
        self.screen.addWidget(self._music_panel())
        lay.addWidget(self.screen, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(24, 10, 24, 14)
        bar.setSpacing(10)
        self.play_btn = self._tool("play", tr("재생 / 일시정지 (Space)"), self.toggle_play)
        bar.addWidget(self.play_btn)
        self.seek = ClickSlider(Qt.Horizontal)
        self.seek.setFocusPolicy(Qt.NoFocus)
        self.seek.sliderMoved.connect(lambda v: self.player.setPosition(v))
        self.seek.sliderPressed.connect(lambda: self.player.setPosition(self.seek.value()))
        bar.addWidget(self.seek, 1)
        self.time = QLabel("00:00 / 00:00")
        self.time.setFont(theme.mono_font(12))
        self.time.setObjectName("Muted")
        bar.addWidget(self.time)
        bar.addSpacing(6)
        self.mute_btn = self._tool("speaker-high", tr("음소거"), self.toggle_mute)
        bar.addWidget(self.mute_btn)
        self.volume = ClickSlider(Qt.Horizontal)
        self.volume.setFocusPolicy(Qt.NoFocus)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        bar.addWidget(self.volume)
        self.full_btn = self._tool("corners-out", tr("전체 화면 (F)"), self.toggle_fullscreen)
        bar.addWidget(self.full_btn)
        lay.addLayout(bar)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.surface)
        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_status)

    def _tool(self, name: str, tip: str, slot) -> QToolButton:
        b = QToolButton()
        b.setIcon(theme.icon(name, color=theme.TEXT))
        b.setIconSize(QSize(20, 20))
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        return b

    COVER = 280

    def _music_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(6)
        self.cover = QLabel()
        self.cover.setFixedSize(self.COVER, self.COVER)
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setStyleSheet(f"background: {theme.CARD}; border-radius: 12px;")
        lay.addWidget(self.cover, 0, Qt.AlignCenter)
        lay.addSpacing(14)
        self.title = label("", "Big")
        self.title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        self.artist = label("", "Muted")
        self.artist.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.artist)
        return panel

    def _show_cover(self, img: QImage | None) -> None:
        if img is not None and not img.isNull():
            pix = QPixmap.fromImage(img).scaled(self.COVER, self.COVER, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        else:
            pix = theme.icon("music-notes", color=theme.FAINT).pixmap(96, 96)
        self.cover.setPixmap(pix)

    def load(self, path: Path, audio: bool = False) -> None:
        self.audio_only = audio
        self.screen.setCurrentIndex(1 if audio else 0)
        self.full_btn.setVisible(not audio)
        if audio:
            self._show_cover(None)
            self.title.setText(path.stem)
            self.artist.setText("")
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def load_bytes(self, data: bytes, name: str, audio: bool = False) -> None:
        self.audio_only = audio
        self.screen.setCurrentIndex(1 if audio else 0)
        self.full_btn.setVisible(not audio)
        if audio:
            self._show_cover(None)
            self.title.setText(name.rpartition(".")[0] or name)
            self.artist.setText("")
        self._buf = QBuffer()
        self._buf.setData(QByteArray(data))
        self._buf.open(QIODevice.ReadOnly)
        self.player.setSourceDevice(self._buf, QUrl(name))
        self.player.play()

    def stop(self) -> None:
        self.set_fullscreen(False)
        self.player.stop()
        self.player.setSource(QUrl())
        self._buf = None

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.player.mediaStatus() == QMediaPlayer.EndOfMedia:
                self.player.setPosition(0)
            self.player.play()

    def toggle_mute(self) -> None:
        self.audio.setMuted(not self.audio.isMuted())
        self.mute_btn.setIcon(theme.icon("speaker-slash" if self.audio.isMuted() else "speaker-high", color=theme.TEXT))

    def seek_by(self, ms: int) -> None:
        self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + ms)))

    @property
    def is_fullscreen(self) -> bool:
        return self.surface.isFullScreen()

    def set_fullscreen(self, on: bool) -> None:
        if on != self.is_fullscreen:
            self.surface.setFullScreen(on)
            self.full_btn.setIcon(theme.icon("corners-in" if on else "corners-out", color=theme.TEXT))
            (self.surface if on else self).setFocus()

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen(not self.is_fullscreen)

    def handle_key(self, e) -> bool:
        key = e.key()
        if key == Qt.Key_Space:
            self.toggle_play()
        elif key in (Qt.Key_Left, Qt.Key_Right) and not e.modifiers():
            self.seek_by(-SEEK_STEP_MS if key == Qt.Key_Left else SEEK_STEP_MS)
        elif key == Qt.Key_F:
            self.toggle_fullscreen()
        elif key == Qt.Key_Escape and self.is_fullscreen:
            self.set_fullscreen(False)
        else:
            return False
        return True

    def keyPressEvent(self, e):  # noqa: N802
        if not self.handle_key(e):
            super().keyPressEvent(e)

    def _on_duration(self, ms: int) -> None:
        self.seek.setRange(0, ms)
        self._on_position(self.player.position())

    def _on_position(self, ms: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        self.time.setText(f"{fmt_ms(ms)} / {fmt_ms(self.player.duration())}")

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.play_btn.setIcon(theme.icon("pause" if playing else "play", color=theme.TEXT))

    @property
    def _cannot_play(self) -> str:
        if self.audio_only:
            return tr("이 음악을 재생할 수 없습니다. (지원하지 않는 코덱일 수 있습니다)")
        return tr("이 영상을 재생할 수 없습니다. (지원하지 않는 코덱일 수 있습니다)")

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.LoadedMedia:
            meta = self.player.metaData()
            if self.audio_only:
                title = meta.stringValue(meta.Key.Title)
                if title:
                    self.title.setText(title)
                who = [meta.stringValue(k) for k in (meta.Key.ContributingArtist, meta.Key.AlbumTitle)]
                self.artist.setText(" · ".join(w for w in who if w))
                cover = meta.value(meta.Key.CoverArtImage) or meta.value(meta.Key.ThumbnailImage)
                self._show_cover(cover if isinstance(cover, QImage) else None)
                return
            size = meta.value(meta.Key.Resolution)
            if size and not size.isEmpty():
                self.loaded.emit(size.width(), size.height())
        elif status == QMediaPlayer.InvalidMedia:
            self.failed.emit(self._cannot_play)

    def _on_error(self, error, text: str = "") -> None:
        if error != QMediaPlayer.NoError and (self.player.source().isValid() or self._buf is not None):
            self.failed.emit(self._cannot_play)


class PdfView(QPdfView):
    failed = Signal(str)
    loaded = Signal(int)

    def __init__(self):
        super().__init__(None)
        self.setPageMode(QPdfView.PageMode.MultiPage)
        self.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.setPageSpacing(12)
        self.setDocumentMargins(QMargins(24, 8, 24, 16))
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"QPdfView {{ background: {theme.BG}; border: none; }}")
        pal = self.palette()
        pal.setColor(QPalette.Dark, QColor(theme.BG))
        self.setPalette(pal)
        self.doc = None
        self._buf = None

    def load(self, src) -> None:
        self.clear()
        try:
            self.doc, self._buf = open_pdf(src)
        except ValueError:
            self.failed.emit(tr("이 PDF를 열 수 없습니다. (암호가 걸렸거나 손상됐을 수 있습니다)"))
            return
        self.setDocument(self.doc)
        self.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.verticalScrollBar().setValue(0)
        self.loaded.emit(self.doc.pageCount())

    def clear(self) -> None:
        self.setDocument(None)
        if self.doc is not None:
            self.doc.close()
            self.doc.deleteLater()
        self.doc, self._buf = None, None

    def _zoom(self, factor: float) -> None:
        self.setZoomMode(QPdfView.ZoomMode.Custom)
        self.setZoomFactor(max(0.1, min(8.0, self.zoomFactor() * factor)))

    def wheelEvent(self, e):  # noqa: N802
        if e.modifiers() & Qt.ControlModifier:
            steps = e.angleDelta().y() / 120
            if steps:
                self._zoom(ZOOM_STEP ** steps)
        else:
            super().wheelEvent(e)

    def keyPressEvent(self, e):  # noqa: N802
        key = e.key()
        if key in (Qt.Key_Plus, Qt.Key_Equal):
            self._zoom(ZOOM_STEP)
        elif key == Qt.Key_Minus:
            self._zoom(1 / ZOOM_STEP)
        elif key == Qt.Key_0:
            self.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        else:
            super().keyPressEvent(e)


class TextView(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setObjectName("Editor")
        font = theme.mono_font(14)
        font.setFamilies([theme.MONO, *theme.sans_families()])
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)


class ViewerPage(QWidget):
    back_requested = Signal()
    folder_requested = Signal(str)
    closed = Signal()

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setObjectName("Center")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.current: str | None = None
        self.memory = False
        self.kind: str | None = None
        self.siblings: list[str] = []
        self.doc: textfile.TextDoc | None = None
        self._info = ""
        self.zip_base: list[tuple[str, str]] = []
        self.zip_entry: str | None = None
        self.zip_mode = 0
        self._entry_token = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.header = NavHeader()
        self.header.back_clicked.connect(self.back_requested.emit)
        self.header.crumb_clicked.connect(self._crumb)
        self.header.forward.setEnabled(False)
        self.back = self.header.back
        self.dirty_mark = label("●", "DirtyMark")
        self.dirty_mark.setToolTip(tr("저장하지 않은 내용이 있습니다"))
        self.dirty_mark.hide()
        self.header.after.addWidget(self.dirty_mark)
        self.header.after.addSpacing(10)
        self.count = label("", "Muted")
        self.header.after.addWidget(self.count)
        head = self.header.right
        self.zip_toggle = SegmentedToggle(["squares-four", "list-bullets"], [tr("격자로 보기"), tr("목록으로 보기")])
        self.zip_toggle.changed.connect(self._set_zip_mode)
        self.zip_toggle.hide()
        head.addWidget(self.zip_toggle)
        head.addSpacing(8)
        self.prev = self._tool("caret-left", tr("이전 파일"), lambda: self.step(-1))
        head.addWidget(self.prev)
        self.next = self._tool("caret-right", tr("다음 파일"), lambda: self.step(1))
        head.addWidget(self.next)
        head.addSpacing(8)
        self.save_btn = button(tr("저장"), "Primary", "floppy-disk", theme.ACCENT_INK)
        self.save_btn.setToolTip(tr("저장 (Ctrl+S)"))
        self.save_btn.clicked.connect(self.save)
        head.addWidget(self.save_btn)
        head.addSpacing(4)
        self.external = self._tool("arrow-square-out", tr("Windows 앱으로 열기"), self.open_external)
        head.addWidget(self.external)
        lay.addWidget(self.header)
        lay.addSpacing(4)

        self.body = QStackedWidget()
        self.image = ImageView()
        self.image.failed.connect(self._show_message)
        self.image.loaded.connect(lambda w, h: self._set_info(f"{w} × {h}"))
        self.image.step_requested.connect(self.step)
        self._video: VideoView | None = None
        self.text = TextView()
        self.text.document().modificationChanged.connect(self._on_modified)
        text_box = QWidget()
        tl = QVBoxLayout(text_box)
        tl.setContentsMargins(24, 0, 24, 16)
        tl.addWidget(self.text)
        self.message = self._message_panel()
        self.pdf = PdfView()
        self.pdf.failed.connect(self._show_message)
        self.pdf.loaded.connect(lambda n: self._set_info(tr("{n}쪽", n=n)))
        self.archive = ArchivePanel(session)
        self.archive.failed.connect(self._show_message)
        self.archive.loaded.connect(self._set_info)
        self.archive.folder_changed.connect(lambda _: self._render_zip_trail())
        self.archive.open_requested.connect(self._open_entry)
        for w in (self.image, text_box, self.message, self.pdf, self.archive):
            self.body.addWidget(w)
        self.text_box = text_box
        lay.addWidget(self.body, 1)

        self.footer = label("", "Footer")
        lay.addWidget(self.footer)

        QShortcut(QKeySequence.Save, self, activated=self.save, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._escape, context=Qt.WidgetWithChildrenShortcut)
        session.scan_changed.connect(self._on_scan)

    def _tool(self, name: str, tip: str, slot) -> QToolButton:
        b = tool_button(name, tip)
        b.clicked.connect(slot)
        return b

    def _message_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setAlignment(Qt.AlignCenter)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(icon_label("file-dotted", theme.FAINT, 56))
        row.addStretch()
        lay.addLayout(row)
        self.message_text = label("", "Muted")
        self.message_text.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.message_text)
        lay.addSpacing(8)
        row = QHBoxLayout()
        row.addStretch()
        open_btn = button(tr("Windows 앱으로 열기"), None, "arrow-square-out")
        open_btn.clicked.connect(self.open_external)
        row.addWidget(open_btn)
        row.addStretch()
        lay.addLayout(row)
        return panel

    @property
    def video(self) -> VideoView:
        if self._video is None:
            self._video = VideoView()
            self._video.failed.connect(self._show_message)
            self._video.loaded.connect(lambda w, h: self._set_info(f"{w} × {h}"))
            self.body.addWidget(self._video)
        return self._video

    @property
    def path(self) -> Path:
        return self.session.root / self.current

    @property
    def dirty(self) -> bool:
        return self.kind == "text" and self.doc is not None and self.text.document().isModified()

    def open_memory(self, name: str, data: bytes, place: tuple[str, str]) -> None:
        self.close_file()
        self.kind = viewer_kind(name)
        self.memory = True
        self.siblings = []
        self.header.set_trail([place, (name, place[1])])
        self.count.setText("")
        for w in (self.prev, self.next, self.save_btn, self.external):
            w.setVisible(False)
        self._info = ""
        self.footer.setText(human(len(data)))
        if self.kind == "pdf":
            self.body.setCurrentWidget(self.pdf)
            self.pdf.load(data)
            self.pdf.setFocus()
        elif self.kind == "archive":
            self._open_archive([place, (name, ZIP_CRUMB)], data)
        else:
            self.body.setCurrentWidget(self.image)
            self.image.load_bytes(data, name)
            self.image.setFocus()

    def open(self, rel: str, siblings: list[str] | None = None) -> None:
        self.close_file()
        self.memory = False
        for w in (self.prev, self.next, self.external):
            w.setVisible(True)
        self.current = rel
        self.kind = viewer_kind(rel)
        self.siblings = siblings if siblings and rel in siblings else [rel]
        name = rel.rpartition("/")[2]
        folder = rel.rpartition("/")[0]
        self.header.set_trail(folder_trail(folder, self.session.drive.name) + [(name, folder)])
        self._update_nav()
        self.save_btn.setVisible(self.kind == "text")
        self.save_btn.setEnabled(False)
        self._info = ""
        self._update_footer()

        if self.kind == "image":
            self.body.setCurrentWidget(self.image)
            self.image.load(self.path)
            self.image.setFocus()
        elif self.kind in ("video", "audio"):
            self.body.setCurrentWidget(self.video)
            self.video.load(self.path, audio=self.kind == "audio")
            self.video.setFocus()
        elif self.kind == "pdf":
            self.body.setCurrentWidget(self.pdf)
            self.pdf.load(self.path)
            self.pdf.setFocus()
        elif self.kind == "archive":
            self._open_archive(folder_trail(folder, self.session.drive.name) + [(name, ZIP_CRUMB)], self.path)
        elif self.kind == "text":
            self._open_text()
        else:
            self._show_message(tr("Secret에서 열 수 없는 형식입니다."))

    def _open_text(self) -> None:
        try:
            self.doc = textfile.load(self.path)
        except TooLarge:
            self._show_message(tr("{v1}MB가 넘는 텍스트는 Windows 앱으로 열어 주세요.", v1=textfile.MAX_EDIT_BYTES // (1024 * 1024)))
            return
        except NotText:
            self._show_message(tr("글자로 읽을 수 없는 파일입니다."))
            return
        except OSError as exc:
            self._show_message(tr("파일을 열 수 없습니다. ({v1})", v1=exc.strerror or exc))
            return
        self.text.setPlainText(self.doc.text)
        self.text.document().setModified(False)
        self.text.moveCursor(self.text.textCursor().MoveOperation.Start)
        self.body.setCurrentWidget(self.text_box)
        self._set_text_info()
        self.text.setFocus()

    def _set_text_info(self) -> None:
        d = self.doc
        enc = {"utf-8": "UTF-8 (BOM)" if d.bom else "UTF-8", "cp949": "CP949", "utf-16": "UTF-16"}.get(d.encoding, d.encoding)
        nl = {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}[d.newline]
        self._set_info(f"{enc} · {nl}")

    def _show_message(self, text: str) -> None:
        if self._video:
            self._video.stop()
        self.message_text.setText(text)
        self.body.setCurrentWidget(self.message)

    def _set_info(self, text: str) -> None:
        self._info = text
        if self.zip_entry is not None:
            e = self.archive.entry(self.zip_entry)
            self.footer.setText(" · ".join(x for x in (text, human(e.size) if e else "", tr("압축 안 · 읽기 전용")) if x))
            return
        if self.memory:
            size = self.footer.text().split(" · ")[-1]
            self.footer.setText(f"{text} · {size}" if text else size)
            return
        self._update_footer()

    def _update_footer(self) -> None:
        parts = []
        try:
            st = self.path.stat()
            parts = [human(st.st_size), datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")]
        except (OSError, TypeError):
            pass
        if self._info:
            parts.insert(0, self._info)
        self.footer.setText(" · ".join(parts))

    def _open_archive(self, base: list[tuple[str, str]], src) -> None:
        self.zip_base = base
        self.zip_entry = None
        self.body.setCurrentWidget(self.archive)
        self.archive.set_mode(self.zip_mode)
        self.zip_toggle.setCurrent(self.zip_mode)
        self.zip_toggle.show()
        self.archive.load(src)
        self.archive.setFocus()

    def _set_zip_mode(self, index: int) -> None:
        self.zip_mode = index
        self.archive.set_mode(index)

    def _render_zip_trail(self) -> None:
        if self.kind != "archive":
            return
        trail = list(self.zip_base)
        parts = self.archive.folder.split("/") if self.archive.folder else []
        trail += [(part, ZIP_CRUMB + "/".join(parts[: i + 1])) for i, part in enumerate(parts)]
        if self.zip_entry:
            trail.append((self.zip_entry.rpartition("/")[2], ""))
        self.header.set_trail(trail)

    def _crumb(self, value: str) -> None:
        if value.startswith(ZIP_CRUMB):
            self._close_entry()
            self.archive.set_folder(value[len(ZIP_CRUMB):])
        else:
            self.folder_requested.emit(value)

    def _open_entry(self, path: str) -> None:
        e = self.archive.entry(path)
        if e is None:
            return
        self._clear_views()
        self.zip_entry = path
        self.zip_toggle.hide()
        self._render_zip_trail()
        self._update_nav()
        self._set_info("")
        kind = viewer_kind(path)
        if e.locked:
            self._show_message(tr("암호가 걸린 파일이라 열 수 없습니다. 압축을 풀어서 여세요."))
            return
        if kind is None:
            self._show_message(tr("Secret에서 열 수 없는 형식입니다. 압축을 풀어서 여세요."))
            return
        if kind == "archive":
            self._show_message(tr("압축 파일 안의 압축 파일은 풀어서 여세요."))
            return
        if e.size > MAX_OPEN_BYTES:
            self._show_message(tr("{v1}MB가 넘어 압축 안에서는 열 수 없습니다. 압축을 풀어서 여세요.", v1=MAX_OPEN_BYTES // (1024 * 1024)))
            return
        self._entry_token += 1
        token, archive = self._entry_token, self.archive.archive
        run_async(lambda: archive.read(path), lambda data: token == self._entry_token and self._show_entry(path, kind, data),
                  lambda exc: token == self._entry_token and self._show_message(tr("압축 안의 이 파일을 읽을 수 없습니다.")))

    def _show_entry(self, path: str, kind: str, data: bytes) -> None:
        name = path.rpartition("/")[2]
        if kind == "image":
            self.body.setCurrentWidget(self.image)
            self.image.load_bytes(data, name)
            self.image.setFocus()
        elif kind == "pdf":
            self.body.setCurrentWidget(self.pdf)
            self.pdf.load(data)
            self.pdf.setFocus()
        elif kind in ("audio", "video"):
            self.body.setCurrentWidget(self.video)
            self.video.load_bytes(data, name, audio=kind == "audio")
            self.video.setFocus()
        elif kind == "text":
            try:
                text = textfile.decode_text(data)
            except TooLarge:
                self._show_message(tr("{v1}MB가 넘는 텍스트는 압축을 풀어서 여세요.", v1=textfile.MAX_EDIT_BYTES // (1024 * 1024)))
                return
            except NotText:
                self._show_message(tr("글자로 읽을 수 없는 파일입니다."))
                return
            self.text.setReadOnly(True)
            self.text.setPlainText(text)
            self.text.document().setModified(False)
            self.body.setCurrentWidget(self.text_box)
            self.text.setFocus()

    def _clear_views(self) -> None:
        self._entry_token += 1
        self.image.clear()
        self.pdf.clear()
        if self._video:
            self._video.stop()
        self.text.setReadOnly(False)
        self.text.setPlainText("")
        self.text.document().setModified(False)
        self._reset_dirty()

    def _close_entry(self) -> None:
        if self.zip_entry is None:
            return
        self._clear_views()
        self.zip_entry = None
        self.body.setCurrentWidget(self.archive)
        self.zip_toggle.show()
        self._render_zip_trail()
        self._update_nav()
        if self.archive.archive:
            self._set_info(self.archive.archive.summary)
        self.archive.setFocus()

    def back_inside(self) -> bool:
        if self.kind != "archive":
            return False
        if self.zip_entry is not None:
            self._close_entry()
            return True
        if self.archive.folder:
            self.archive.set_folder(self.archive.folder.rpartition("/")[0])
            return True
        return False

    def _zip_files(self) -> list[str]:
        return [p for p in self.archive.files_here() if viewer_kind(p) not in (None, "archive")]

    def _update_nav(self) -> None:
        if self.kind == "archive" and self.zip_entry is not None:
            files = self._zip_files()
            i = files.index(self.zip_entry) if self.zip_entry in files else -1
            n = len(files)
        else:
            files = self.siblings
            i = self.siblings.index(self.current) if self.current in self.siblings else -1
            n = len(self.siblings)
        self.count.setText(f"{i + 1} / {n}" if n > 1 and i >= 0 else "")
        self.prev.setEnabled(i > 0)
        self.next.setEnabled(0 <= i < n - 1)

    def step(self, delta: int) -> None:
        if self.kind == "archive" and self.zip_entry is not None:
            files = self._zip_files()
            if self.zip_entry in files:
                i = files.index(self.zip_entry) + delta
                if 0 <= i < len(files):
                    self._open_entry(files[i])
            return
        if self.current not in self.siblings:
            return
        i = self.siblings.index(self.current) + delta
        if 0 <= i < len(self.siblings) and self.maybe_leave():
            self.open(self.siblings[i], self.siblings)

    def maybe_leave(self) -> bool:
        if not self.dirty:
            return True
        answer = ask_save(self, self.current.rpartition("/")[2])
        if answer == "save":
            return self.save()
        return answer == "discard"

    def close_file(self) -> None:
        self._entry_token += 1
        self.image.clear()
        self.pdf.clear()
        self.archive.clear()
        self.zip_entry = None
        self.zip_base = []
        self.zip_toggle.hide()
        if self._video:
            self._video.stop()
        self.text.setReadOnly(False)
        self.text.setPlainText("")
        self.text.document().setModified(False)
        self._reset_dirty()
        self.doc = None
        self.current = None
        self.kind = None
        self.memory = False

    def _escape(self) -> None:
        if self._video and self._video.is_fullscreen:
            self.video.set_fullscreen(False)
        else:
            self.back_requested.emit()

    def open_external(self) -> None:
        if self.current:
            open_external(self.path)

    def _reset_dirty(self) -> None:
        self.dirty_mark.hide()
        self.save_btn.setEnabled(False)

    def _on_modified(self, modified: bool) -> None:
        self.dirty_mark.setVisible(modified and self.kind == "text")
        self.save_btn.setEnabled(modified)

    def _on_scan(self) -> None:
        if self.current is None or self.dirty:
            return
        scan = self.session.scan
        if self.current not in scan.files and self.current not in scan.excluded:
            self.close_file()
            self.closed.emit()

    def save(self) -> bool:
        if not self.dirty:
            return True
        content = self.text.toPlainText()
        try:
            try:
                new = textfile.save(self.path, self.doc, content)
            except ChangedOnDisk:
                if not ask(self, tr("저장"), tr("열어 둔 사이에 다른 곳에서 이 파일이 바뀌었습니다.\n지금 내용으로 덮어쓸까요?"),
                           yes=tr("덮어쓰기"), danger=True):
                    return False
                new = textfile.save(self.path, self.doc, content, force=True)
        except OSError as exc:
            inform(self, tr("저장"), tr("저장하지 못했습니다. ({v1})", v1=exc.strerror or exc), error=True)
            return False
        self.doc = new
        self.text.document().setModified(False)
        self._set_text_info()
        self._update_footer()
        self.session.log(tr("저장했습니다: {current}", current=self.current))
        if new.switched_to_utf8:
            inform(self, tr("저장"), tr("옛 메모장 형식(CP949)으로 쓸 수 없는 글자가 있어 UTF-8로 저장했습니다."))
        QTimer.singleShot(0, self.session.refresh_scan)
        return True
