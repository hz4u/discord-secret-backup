from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.strength import MEDIUM, STRONG, WEAK, evaluate
from ..i18n import tr
from . import theme


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


ICON_SIZE = 16
ICON_GAP = 4


class _GapIcon(QIconEngine):
    def __init__(self, icon: QIcon, gap: int):
        super().__init__()
        self.icon, self.gap = icon, gap

    def paint(self, painter, rect, mode, state):  # noqa: N802
        side = min(rect.height(), rect.width() - self.gap)
        self.icon.paint(painter, QRect(rect.x(), rect.y() + (rect.height() - side) // 2, side, side), Qt.AlignCenter, mode, state)

    def pixmap(self, size, mode, state):  # noqa: N802
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        self.paint(p, QRect(QPoint(0, 0), size), mode, state)
        p.end()
        return pm

    def clone(self):
        return _GapIcon(self.icon, self.gap)


def set_icon(b: QPushButton, name: str, color: str = theme.TEXT) -> None:
    b.setIcon(QIcon(_GapIcon(theme.icon(name, color=color, color_disabled=theme.FAINT), ICON_GAP)))
    b.setIconSize(QSize(ICON_SIZE + ICON_GAP, ICON_SIZE))


def button(text: str, name: str | None = None, icon: str | None = None, icon_color: str = theme.TEXT) -> QPushButton:
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    if icon:
        set_icon(b, icon, icon_color)
    b.setCursor(Qt.PointingHandCursor)
    return b


def joined(*widgets) -> QFrame:
    box = QFrame()
    box.setObjectName("Joined")
    box.setFixedHeight(40)
    row = QHBoxLayout(box)
    row.setContentsMargins(1, 1, 1, 1)
    row.setSpacing(0)
    last = len(widgets) - 1
    for i, w in enumerate(widgets):
        if i:
            line = QFrame()
            line.setObjectName("JoinedLine")
            line.setFixedWidth(1)
            row.addWidget(line)
        w.setFixedHeight(38)
        radius = []
        if i == 0:
            radius += ["border-top-left-radius: 7px;", "border-bottom-left-radius: 7px;"]
        if i == last:
            radius += ["border-top-right-radius: 7px;", "border-bottom-right-radius: 7px;"]
        if radius and not isinstance(w, QLineEdit):
            w.setStyleSheet(" ".join(radius))
        row.addWidget(w, 1 if isinstance(w, QLineEdit) else 0, Qt.AlignVCenter)
    return box


class SegmentedToggle(QWidget):
    changed = Signal(int)

    def __init__(self, icons: list[str], tips: list[str], seg_width: int = 44, height: int = 40):
        super().__init__()
        self.icons = [theme.icon(n, color=theme.TEXT) for n in icons]
        self.tips = tips
        self.seg = seg_width
        self.current = 0
        self._hover = -1
        self.setFixedSize(seg_width * len(icons), height)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def setCurrent(self, index: int) -> None:  # noqa: N802
        if index != self.current:
            self.current = index
            self.update()
            self.changed.emit(index)

    def _index_at(self, x: float) -> int:
        return max(0, min(len(self.icons) - 1, int(x // self.seg)))

    def mousePressEvent(self, e):  # noqa: N802
        self.setCurrent(self._index_at(e.position().x()))

    def mouseMoveEvent(self, e):  # noqa: N802
        i = self._index_at(e.position().x())
        if i != self._hover:
            self._hover = i
            self.setToolTip(self.tips[i])
            self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = -1
        self.update()

    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(outer, 8, 8)
        p.fillPath(clip, QColor(theme.PANEL))
        p.save()
        p.setClipPath(clip)
        for i in range(len(self.icons)):
            seg = QRectF(i * self.seg, 0, self.seg, self.height())
            if i == self.current:
                p.fillRect(seg, QColor(theme.SELECTED))
            elif i == self._hover:
                p.fillRect(seg, QColor(theme.CARD_HI))
        p.restore()
        p.setPen(QColor(theme.BORDER))
        for i in range(1, len(self.icons)):
            x = i * self.seg + 0.5
            p.drawLine(QPointF(x, 1), QPointF(x, self.height() - 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(clip)
        size = 20
        for i, ic in enumerate(self.icons):
            cx = i * self.seg + (self.seg - size) / 2
            cy = (self.height() - size) / 2
            p.drawPixmap(int(cx), int(cy), ic.pixmap(size, size))
        p.end()


class ProgressButton(QPushButton):
    def __init__(self, text: str, name: str = "Primary"):
        super().__init__(text)
        self.setObjectName(name)
        self._progress: float | None = None

    @property
    def progress(self) -> float | None:
        return self._progress

    def set_progress(self, value: float | None) -> None:
        self._progress = None if value is None else max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, event):  # noqa: N802
        if self._progress is None:
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        shape = QPainterPath()
        shape.addRoundedRect(r, 8, 8)
        p.fillPath(shape, QColor(theme.BUTTON))
        filled = QRectF(r.x(), r.y(), r.width() * self._progress, r.height())
        rest = QRectF(filled.right(), r.y(), r.width() - filled.width(), r.height())
        p.setFont(self.font())
        for area, fill, ink in ((filled, theme.ACCENT, theme.ACCENT_INK), (rest, None, theme.TEXT)):
            p.save()
            p.setClipRect(area)
            if fill:
                p.fillPath(shape, QColor(fill))
            p.setPen(QColor(ink))
            p.drawText(r, Qt.AlignCenter, self.text())
            p.restore()
        p.end()


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)


def icon_label(name: str, color: str = theme.MUTED, size: int = 20) -> QLabel:
    w = QLabel()
    w.setPixmap(theme.icon(name, color=color).pixmap(size, size))
    w.setFixedSize(size, size)
    return w


class PasswordField(QWidget):
    changed = Signal()
    submitted = Signal()

    def __init__(self, placeholder: str = tr("비밀번호")):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText(placeholder)
        self._eye = self.edit.addAction(theme.icon("eye"), QLineEdit.TrailingPosition)
        self._eye.setToolTip(tr("보기"))
        self._eye.triggered.connect(self._toggle)
        self.edit.textChanged.connect(self.changed)
        self.edit.returnPressed.connect(self.submitted)
        lay.addWidget(self.edit)

    def _toggle(self) -> None:
        shown = self.edit.echoMode() == QLineEdit.Password
        self.edit.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        self._eye.setIcon(theme.icon("eye-slash" if shown else "eye"))
        self._eye.setToolTip(tr("숨기기") if shown else tr("보기"))

    def text(self) -> str:
        return self.edit.text()

    def clear(self) -> None:
        self.edit.clear()

    def setFocus(self) -> None:  # noqa: N802
        self.edit.setFocus()


class StrengthMeter(QWidget):
    _STYLE = {WEAK: (1, theme.DANGER), MEDIUM: (2, theme.WARN), STRONG: (3, theme.OK)}

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.segments = []
        for _ in range(3):
            seg = QFrame()
            seg.setFixedSize(40, 5)
            self.segments.append(seg)
            lay.addWidget(seg)
        self.text = label("", "Faint")
        lay.addSpacing(8)
        lay.addWidget(self.text)
        lay.addStretch()
        self.update_for("")

    def update_for(self, password: str) -> None:
        if password:
            s = evaluate(password)
            filled, color = self._STYLE[s.level]
            text = s.label
        else:
            filled, color, text = 0, theme.BORDER, ""
        for i, seg in enumerate(self.segments):
            seg.setStyleSheet(f"background: {color if i < filled else theme.BORDER}; border-radius: 2px;")
        self.text.setText(text)
        self.text.setStyleSheet(f"color: {color};")


class NewPasswordFields(QWidget):
    changed = Signal()

    def __init__(self, first_label: str = tr("메인 비밀번호"), form: QFormLayout | None = None):
        super().__init__()
        self.pw1, self.pw2 = PasswordField(tr("12자 이상, 문장형 추천")), PasswordField(tr("같은 비밀번호"))
        self.meter = StrengthMeter()
        self.mismatch = label("", "ErrorText")
        if form is not None:
            status = QWidget()
            row = QHBoxLayout(status)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(self.meter)
            row.addWidget(self.mismatch, 1)
            form.addRow(label(first_label, "SectionLabel"), self.pw1)
            form.addRow(label(tr("한 번 더"), "SectionLabel"), self.pw2)
            form.addRow("", status)
        else:
            grid = QGridLayout(self)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(6)
            grid.addWidget(label(first_label, "SectionLabel"), 0, 0)
            grid.addWidget(label(tr("한 번 더"), "SectionLabel"), 0, 1)
            grid.addWidget(self.pw1, 1, 0)
            grid.addWidget(self.pw2, 1, 1)
            grid.addWidget(self.meter, 2, 0)
            grid.addWidget(self.mismatch, 2, 1)
        for pw in (self.pw1, self.pw2):
            pw.changed.connect(self._changed)

    def _changed(self) -> None:
        self.meter.update_for(self.pw1.text())
        bad = bool(self.pw2.text()) and self.pw1.text() != self.pw2.text()
        self.mismatch.setText(tr("비밀번호가 서로 다릅니다") if bad else "")
        self.changed.emit()

    def valid(self) -> bool:
        return bool(self.pw1.text()) and self.pw1.text() == self.pw2.text()

    def text(self) -> str:
        return self.pw1.text()

    def clear(self) -> None:
        self.pw1.clear()
        self.pw2.clear()

    def is_weak(self) -> bool:
        return evaluate(self.pw1.text()).level == WEAK


class Dialog(QDialog):
    def __init__(self, parent, width: int = 520):
        super().__init__(parent)
        self.setWindowTitle("Secret")
        self.setObjectName("Dialog")
        self.setMinimumWidth(width)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(28, 24, 28, 24)
        self.body.setSpacing(12)

    def showEvent(self, event):  # noqa: N802
        theme.dark_titlebar(self)
        super().showEvent(event)

    def heading(self, text: str, sub: str = "", icon: str | None = None, color: str = theme.ACCENT) -> None:
        row = QHBoxLayout()
        if icon:
            row.addWidget(icon_label(icon, color, 28))
            row.addSpacing(6)
        row.addWidget(label(text, "Big"))
        row.addStretch()
        self.body.addLayout(row)
        if sub:
            self.body.addWidget(label(sub, "Muted", wrap=True))
        self.body.addSpacing(6)


def _box(parent, icon, title: str, text: str) -> QMessageBox:
    box = QMessageBox(icon, "Secret", f"<b>{title}</b>", parent=parent)
    box.setInformativeText(text.replace("\n", "<br>"))
    return box


def ask(parent, title: str, text: str, yes: str = tr("확인"), danger: bool = False) -> bool:
    box = _box(parent, QMessageBox.Warning if danger else QMessageBox.Question, title, text)
    ok = box.addButton(yes, QMessageBox.AcceptRole)
    ok.setObjectName("Danger" if danger else "Primary")
    box.addButton(tr("취소"), QMessageBox.RejectRole)
    theme.dark_titlebar(box)
    box.exec()
    return box.clickedButton() is ok


def ask_save(parent, name: str) -> str:
    box = _box(parent, QMessageBox.Question, tr("저장하지 않은 내용"), tr("{name}에 저장하지 않은 내용이 있습니다.\n저장할까요?", name=name))
    save = box.addButton(tr("저장"), QMessageBox.AcceptRole)
    save.setObjectName("Primary")
    discard = box.addButton(tr("저장 안 함"), QMessageBox.DestructiveRole)
    box.addButton(tr("취소"), QMessageBox.RejectRole)
    theme.dark_titlebar(box)
    box.exec()
    clicked = box.clickedButton()
    return "save" if clicked is save else "discard" if clicked is discard else "cancel"


def inform(parent, title: str, text: str, error: bool = False) -> None:
    box = _box(parent, QMessageBox.Critical if error else QMessageBox.Information, title, text)
    box.addButton(tr("확인"), QMessageBox.AcceptRole)
    theme.dark_titlebar(box)
    box.exec()


def confirm_weak(parent, fields: NewPasswordFields) -> bool:
    if not fields.is_weak():
        return True
    return ask(parent, tr("약한 비밀번호"), tr("약한 비밀번호입니다.\n12자 이상, 흔하지 않은 문장형 비밀번호를 권장합니다.\n\n그래도 계속할까요?"), yes=tr("계속"))
