from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from ..i18n import tr
from . import theme

ROW_HEIGHT = 40
CRUMB_MAX_PX = 220
LAST_CRUMB_MAX_PX = 420
TOOL_SIZE = 36
TOOL_ICON = 20


def tool_button(icon: QIcon | str, tip: str, color: str = theme.TEXT) -> QToolButton:
    if isinstance(icon, str):
        icon = theme.icon(icon, color=color, color_disabled=theme.FAINT)
    b = QToolButton()
    b.setIcon(theme.centered(icon))
    b.setIconSize(QSize(TOOL_ICON, TOOL_ICON))
    b.setFixedSize(TOOL_SIZE, TOOL_SIZE)
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


def icon_pixmap_label(name: str, color: str, size: int = 22) -> QLabel:
    w = QLabel()
    w.setPixmap(theme.icon(name, color=color).pixmap(size, size))
    return w


class NavHeader(QWidget):
    back_clicked = Signal()
    forward_clicked = Signal()
    crumb_clicked = Signal(str)

    def __init__(self, place_icon: str = "folder-open"):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 22, 24, 8)
        self.back = tool_button("arrow-left", tr("뒤로 (Alt+←)"))
        self.back.clicked.connect(self.back_clicked)
        row.addWidget(self.back)
        self.forward = tool_button("arrow-right", tr("앞으로 (Alt+→)"))
        self.forward.clicked.connect(self.forward_clicked)
        row.addWidget(self.forward)
        row.addSpacing(8)
        row.addWidget(icon_pixmap_label(place_icon, theme.TEXT))
        self.crumbs = QHBoxLayout()
        self.crumbs.setSpacing(0)
        row.addLayout(self.crumbs)
        self.after = QHBoxLayout()
        row.addLayout(self.after)
        row.addStretch()
        self.right = QHBoxLayout()
        row.addLayout(self.right)
        strut = QWidget()
        strut.setFixedSize(0, ROW_HEIGHT)
        row.addWidget(strut)

    def set_trail(self, trail: list[tuple[str, str]]) -> None:
        while self.crumbs.count():
            w = self.crumbs.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        for i, (name, rel) in enumerate(trail):
            if i:
                self.crumbs.addWidget(icon_pixmap_label("caret-right", theme.FAINT))
            last = i == len(trail) - 1
            b = QToolButton()
            b.setObjectName("Crumb")
            b.ensurePolished()
            limit = LAST_CRUMB_MAX_PX if last else CRUMB_MAX_PX
            shown = b.fontMetrics().elidedText(name, Qt.ElideMiddle, limit)
            b.setText(shown)
            if shown != name:
                b.setToolTip(name)
            b.setCursor(Qt.PointingHandCursor)
            b.setEnabled(not last)
            b.setStyleSheet("" if last else f"color: {theme.MUTED};")
            b.clicked.connect(lambda _=False, r=rel: self.crumb_clicked.emit(r))
            self.crumbs.addWidget(b)


def folder_trail(folder: str, root: str = "USB") -> list[tuple[str, str]]:
    parts = folder.split("/") if folder else []
    return [(root, "")] + [(p, "/".join(parts[: i + 1])) for i, p in enumerate(parts)]
