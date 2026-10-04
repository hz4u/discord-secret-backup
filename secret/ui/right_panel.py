import base64
import html
from datetime import datetime

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from ..core.sync_engine import SyncProgress
from ..i18n import tr
from . import theme
from .sidebar import human
from .widgets import Card, ProgressButton, button, icon_label, label

PANEL_WIDTH = 320
IDLE_TEXT = tr("지금 동기화")
BUTTON_GAP = 8
DISCORD_BLURPLE = "#5865F2"
SPINNER = "|/-\\"
FLASH_MS = 2500
LOG_LINES = 500


def circle_pixmap(src: QPixmap, size: int = 48) -> QPixmap:
    ratio = 2
    out = QPixmap(size * ratio, size * ratio)
    out.fill(Qt.transparent)
    scaled = src.scaled(out.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addEllipse(0, 0, out.width(), out.height())
    p.setClipPath(path)
    p.drawPixmap((out.width() - scaled.width()) // 2, (out.height() - scaled.height()) // 2, scaled)
    p.end()
    out.setDevicePixelRatio(ratio)
    return out


def discord_avatar(size: int = 48) -> QPixmap:
    ratio = 2
    out = QPixmap(size * ratio, size * ratio)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(DISCORD_BLURPLE))
    p.drawEllipse(out.rect())
    glyph = out.width() * 3 // 5
    offset = (out.width() - glyph) // 2
    theme.icon("discord-logo", color="#FFFFFF").paint(p, QRect(offset, offset, glyph, glyph))
    p.end()
    out.setDevicePixelRatio(ratio)
    return out


def fmt_time(iso: str | None) -> str:
    if not iso:
        return tr("아직 없음")
    return datetime.fromisoformat(iso).strftime("%Y.%m.%d %H:%M")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return tr("계산 중")
    seconds = int(seconds)
    if seconds < 60:
        return tr("{seconds}초", seconds=seconds)
    if seconds < 3600:
        return tr("{v1}분 {v2}초", v1=seconds // 60, v2=seconds % 60)
    return tr("{v1}시간 {v2}분", v1=seconds // 3600, v2=seconds % 3600 // 60)


class RightPanel(QFrame):
    connect_requested = Signal()
    disconnect_requested = Signal()
    unlock_requested = Signal()
    restore_requested = Signal()
    sync_requested = Signal()
    stop_requested = Signal()

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.setObjectName("RightPanel")
        self.setFixedWidth(PANEL_WIDTH)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 0)
        lay.setSpacing(0)

        self.discord = Card()
        self.discord.body.setSpacing(16)
        self.default_avatar = discord_avatar()
        head = QHBoxLayout()
        self.avatar = QLabel()
        self.avatar.setFixedSize(48, 48)
        head.addWidget(self.avatar)
        head.addSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label(tr("Discord 연동"), "CardTitle"))
        self.status = label("", None)
        self.status.setStyleSheet("font-size: 13px;")
        col.addWidget(self.status)
        head.addLayout(col, 1)
        self.discord.body.addLayout(head)
        actions = QHBoxLayout()
        actions.setSpacing(BUTTON_GAP)
        self.restore_button = button(tr("디스코드에서 복원"))
        self.restore_button.clicked.connect(self.restore_requested)
        self.discord_action = button("")
        self.discord_action.clicked.connect(self._discord_action)
        for b in (self.restore_button, self.discord_action):
            b.setStyleSheet("padding: 0 12px;")
            actions.addWidget(b, 1)
        buttons = QVBoxLayout()
        buttons.setSpacing(BUTTON_GAP)
        buttons.addLayout(actions)
        self.discord.body.addLayout(buttons)

        sync_col = QVBoxLayout()
        sync_col.setSpacing(BUTTON_GAP)
        sync_row = QHBoxLayout()
        sync_row.setSpacing(BUTTON_GAP)
        self.sync_button = ProgressButton(IDLE_TEXT)
        self.sync_button.clicked.connect(self._sync_clicked)
        sync_row.addWidget(self.sync_button, 1)
        self.stop_button = button(tr("중지"), icon="stop", icon_color=theme.TEXT)
        self.stop_button.clicked.connect(self.stop_requested)
        self.stop_button.hide()
        sync_row.addWidget(self.stop_button)
        sync_col.addLayout(sync_row)
        buttons.addLayout(sync_col)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        self.discord.body.addWidget(line)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(label(tr("대기 중인 변경"), "SectionLabel"), 0, 0, 1, 3)
        self.counts = {}
        for i, (key, text, icon, color) in enumerate((
            ("new", tr("새로운 파일"), "plus-circle", theme.MUTED),
            ("changed", tr("바뀐 파일"), "pencil-simple", theme.MUTED),
            ("deleted", tr("삭제된 파일"), "trash", theme.MUTED),
            ("excluded", tr("백업 제외"), "prohibit", theme.MUTED),
        ), start=1):
            grid.addWidget(icon_label(icon, color, 16), i, 0)
            grid.addWidget(label(text, None), i, 1)
            value = label("–", None)
            value.setStyleSheet("font-weight: 600;")
            value.setAlignment(Qt.AlignRight)
            grid.addWidget(value, i, 2)
            self.counts[key] = value
        grid.setColumnStretch(1, 1)
        self.discord.body.addLayout(grid)
        self.sync_info = label("", "Faint")
        self.discord.body.addWidget(self.sync_info)
        lay.addWidget(self.discord)
        lay.addSpacing(8)
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.timeout.connect(self._end_flash)
        self._running = False
        log = Card()
        log.body.setContentsMargins(0, 0, 0, 0)
        log.body.setSpacing(0)
        title = label(tr("로그"), "CardTitle")
        title.setContentsMargins(16, 14, 16, 12)
        log.body.addWidget(title)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        log.body.addWidget(line)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_LINES)
        self.log_view.setPlaceholderText(tr("아직 기록이 없습니다."))
        log.body.addWidget(self.log_view, 1)
        lay.addSpacing(8)
        lay.addWidget(log, 1)
        lay.addSpacing(16)
        self._busy: tuple[str, str] | None = None
        self._spin = 0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(120)
        self._busy_timer.timeout.connect(self._tick_busy)
        session.log_event.connect(self.add_log)

        session.lock_changed.connect(self.refresh)
        session.settings_changed.connect(self.refresh)
        session.scan_changed.connect(self.refresh)
        session.usb_changed.connect(lambda _: self.refresh())
        self.set_running(False)
        self.refresh()

    def refresh(self) -> None:
        s = self.session
        st = s.settings
        connected = bool(st and st.connected)
        if not s.has_config:
            self._set_status(theme.FAINT, tr("연결 안 됨"))
            self._set_action(tr("연결하기"), primary=True)
        elif not s.unlocked:
            self._set_status(theme.FAINT, tr("잠김"))
            self._set_action(tr("잠금 해제"), primary=True)
        elif connected:
            self._set_status(theme.OK, tr("연결됨 - {guild_name}", guild_name=st.guild_name) if st.guild_name else tr("연결됨"))
            self._set_action(tr("연결 해제"), primary=False)
        else:
            self._set_status(theme.FAINT, tr("연결 안 됨"))
            self._set_action(tr("연결하기"), primary=True)
        self._set_avatar(st.guild_icon if connected else "")
        p = s.pending
        for key, w in self.counts.items():
            w.setText(str(getattr(p, key)) if p else "–")
        if self._running or self._flash.isActive():
            return
        self.sync_info.setText(tr("마지막 동기화 {v1}", v1=fmt_time(st.last_sync)) if s.unlocked else "")
        self.sync_info.setVisible(bool(self.sync_info.text()))
        self.sync_button.setEnabled(s.usb_present and (not s.unlocked or s.connected))

    @staticmethod
    def _log_html(stamp: str, text: str, color: str, spin: str = "") -> str:
        tail = f'&nbsp;<span style="font-family:\'{theme.MONO}\'; color:{theme.MUTED}">{html.escape(spin)}</span>' if spin else ""
        return f'<span style="color:{theme.FAINT}">{stamp}</span>&nbsp;&nbsp;<span style="color:{color}">{html.escape(text)}</span>{tail}'

    def add_log(self, text: str, level: str = "info") -> None:
        self._end_busy()
        color = {"warn": theme.WARN, "error": theme.DANGER}.get(level, theme.TEXT)
        stamp = datetime.now().strftime("%H:%M:%S")
        if level == "busy":
            self._busy = (stamp, text)
            self._spin = 0
            self.log_view.appendHtml(self._log_html(stamp, text, color, SPINNER[0]))
            self._busy_timer.start()
        else:
            self.log_view.appendHtml(self._log_html(stamp, text, color))
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _replace_last_line(self, html_text: str) -> None:
        cursor = QTextCursor(self.log_view.document().lastBlock())
        cursor.movePosition(QTextCursor.StartOfBlock)
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.insertHtml(html_text)

    def _tick_busy(self) -> None:
        if not self._busy:
            return
        self._spin = (self._spin + 1) % len(SPINNER)
        stamp, text = self._busy
        self._replace_last_line(self._log_html(stamp, text, theme.TEXT, SPINNER[self._spin]))

    def _end_busy(self) -> None:
        if not self._busy:
            return
        self._busy_timer.stop()
        stamp, text = self._busy
        self._busy = None
        self._replace_last_line(self._log_html(stamp, text, theme.TEXT))

    def _set_status(self, color: str, text: str) -> None:
        text = self.status.fontMetrics().elidedText(text, Qt.ElideRight, 190)
        self.status.setText(f'<span style="color:{color}">●</span>&nbsp;&nbsp;{text}')
        self.status.setToolTip(text)

    def _set_action(self, text: str, primary: bool) -> None:
        self.discord_action.setText(text)
        self.discord_action.setObjectName("Primary" if primary else "")
        self.discord_action.style().unpolish(self.discord_action)
        self.discord_action.style().polish(self.discord_action)

    def _set_avatar(self, icon_b64: str) -> None:
        pix = QPixmap()
        if icon_b64 and pix.loadFromData(base64.b64decode(icon_b64)):
            self.avatar.setPixmap(circle_pixmap(pix))
        else:
            self.avatar.setPixmap(self.default_avatar)

    def _discord_action(self) -> None:
        s = self.session
        if s.has_config and not s.unlocked:
            self.unlock_requested.emit()
        elif s.connected:
            self.disconnect_requested.emit()
        else:
            self.connect_requested.emit()

    def set_checking(self) -> None:
        self._running = True
        self._flash.stop()
        self.stop_button.setVisible(True)
        self.stop_button.setEnabled(True)
        self.sync_button.set_progress(None)
        self.sync_button.setText(tr("바뀐 내용 확인 중..."))
        self.sync_info.setText(tr("바뀐 내용을 확인한 뒤 보여 드립니다"))

    def set_running(self, running: bool) -> None:
        self._running = running
        self._flash.stop()
        self.stop_button.setVisible(running)
        self.stop_button.setEnabled(True)
        self.sync_button.set_progress(0.0 if running else None)
        if running:
            self.sync_button.setText(tr("동기화 중... 0%"))
            self.sync_info.setText(tr("올리는 중"))
        else:
            self.sync_button.setText(IDLE_TEXT)
            self.refresh()

    def set_progress(self, p: SyncProgress) -> None:
        if not self._running:
            return
        if p.total_bytes:
            ratio = p.done_bytes / p.total_bytes
            if self.sync_button.progress is not None:
                self.sync_button.set_progress(ratio)
                self.sync_button.setText(tr("동기화 중... {v1}%", v1=int(ratio * 100)))
        if p.waiting >= 1:
            self.sync_info.setText(tr("디스코드 대기 중 {waiting:.0f}초", waiting=p.waiting))
        elif p.total_files and p.speed:
            self.sync_info.setText(tr("{done_files}/{total_files}개 · {size}/s · 남은 시간 {v1}", done_files=p.done_files, total_files=p.total_files, size=human(p.speed), v1=fmt_duration(p.eta)))
        elif p.total_files:
            self.sync_info.setText(tr("{done_files}/{total_files}개", done_files=p.done_files, total_files=p.total_files))

    def stopping(self) -> None:
        self.stop_button.setEnabled(False)
        self.sync_button.setText(tr("멈추는 중..."))

    def finish(self, text: str) -> None:
        self.set_running(False)
        self.sync_button.setText(text)
        self._flash.start(FLASH_MS)

    def _sync_clicked(self) -> None:
        if not (self._running or self._flash.isActive()):
            self.sync_requested.emit()

    def _end_flash(self) -> None:
        self.sync_button.setText(IDLE_TEXT)
        self.refresh()
