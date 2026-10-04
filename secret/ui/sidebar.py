import ctypes
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QProgressBar,
    QPushButton,
    QStyledItemDelegate,
    QTreeView,
    QVBoxLayout,
)

from ..i18n import tr
from . import theme
from .widgets import icon_label, label

KIND_MENU = (
    (None, tr("전체 보기"), "house"),
    ("image", tr("이미지"), "image"),
    ("video", tr("영상"), "film-strip"),
    ("document", tr("문서"), "file-text"),
    ("music", tr("음악"), "music-notes"),
    ("other", tr("기타"), "dots-three-circle"),
)
REL_ROLE = Qt.UserRole + 1


def volume_label(root: Path) -> str:
    if sys.platform != "win32":
        return ""
    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root.anchor, buf, 261, None, None, None, None, 0)
    return buf.value if ok else ""


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class _BadgeDelegate(QStyledItemDelegate):
    def __init__(self, sidebar):
        super().__init__(sidebar)
        self.sidebar = sidebar

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        pending = self.sidebar.pending
        count = pending.per_folder.get(index.data(REL_ROLE), 0) if pending else 0
        if not count:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        text = f"↑{count}"
        font = QFont(painter.font())
        font.setPixelSize(11)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        w = painter.fontMetrics().horizontalAdvance(text) + 12
        r = QRect(option.rect.right() - w - 8, option.rect.center().y() - 9, w, 18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.ACCENT_SOFT))
        painter.drawRoundedRect(r, 9, 9)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(r, Qt.AlignCenter, text)
        painter.restore()


class Sidebar(QFrame):
    kind_selected = Signal(object)
    folder_selected = Signal(str)
    tools_clicked = Signal()
    settings_clicked = Signal()
    trash_clicked = Signal()

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.pending = None
        self.setObjectName("Sidebar")
        self.setFixedWidth(280)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        card = QFrame()
        card.setObjectName("UsbCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(22, 18, 18, 18)
        row.addWidget(icon_label(session.drive.icon, theme.TEXT, 34))
        row.addSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label(session.drive.title, "UsbTitle"))
        vol = volume_label(session.root)
        at_drive_root = session.root == Path(session.root.anchor)
        where = session.root.anchor if at_drive_root else str(session.root)
        self.usb_sub = label(f"{where} ({vol})" if vol and at_drive_root else where, "UsbSub")
        self.usb_sub.setToolTip(str(session.root))
        col.addWidget(self.usb_sub)
        row.addLayout(col, 1)
        lay.addWidget(card)

        nav = QVBoxLayout()
        nav.setContentsMargins(14, 14, 14, 8)
        nav.setSpacing(2)
        self.kind_group = QButtonGroup(self)
        self.kind_group.setExclusive(True)
        self.kind_buttons = {}
        for kind, text, icon in KIND_MENU:
            b = QPushButton(f"  {text}")
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setIcon(theme.icon(icon, color=theme.TEXT))
            b.setIconSize(QSize(20, 20))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=kind: self.kind_selected.emit(k))
            self.kind_group.addButton(b)
            self.kind_buttons[kind] = b
            nav.addWidget(b)
        self.kind_buttons[None].setChecked(True)
        lay.addLayout(nav)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER}; margin: 0 22px;")
        lay.addWidget(line)
        lay.addSpacing(8)
        section = label(tr("폴더"), "SectionLabel")
        section.setContentsMargins(14, 0, 0, 0)
        lay.addWidget(section)

        self.model = QStandardItemModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setAnimated(False)
        self.tree.setEditTriggers(QTreeView.NoEditTriggers)
        self.tree.setItemDelegate(_BadgeDelegate(self))
        self.tree.clicked.connect(lambda idx: self.folder_selected.emit(idx.data(REL_ROLE)))
        self.tree.setContentsMargins(0, 0, 0, 0)
        tree_box = QVBoxLayout()
        tree_box.setContentsMargins(0, 0, 0, 0)
        tree_box.addWidget(self.tree)
        lay.addLayout(tree_box, 1)

        bottom = QFrame()
        b = QVBoxLayout(bottom)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(6)
        trash = QPushButton(tr("   휴지통"))
        trash.setObjectName("NavSquare")
        trash.setCheckable(True)
        self.trash_button = trash
        trash.setIcon(theme.icon("trash", color=theme.TEXT))
        trash.setIconSize(QSize(20, 20))
        trash.setToolTip(tr("동기화할 때 지운 파일 (디스코드에 남은 것)"))
        trash.setCursor(Qt.PointingHandCursor)
        trash.clicked.connect(self.trash_clicked)
        b.addWidget(trash)
        tools = QPushButton(tr("   도구"))
        tools.setObjectName("NavSquare")
        tools.setCheckable(True)
        self.tools_button = tools
        tools.setIcon(theme.icon("wrench", color=theme.TEXT))
        tools.setIconSize(QSize(20, 20))
        tools.setToolTip(tr("텍스트 암호화·복호화, 파일 암호화"))
        tools.setCursor(Qt.PointingHandCursor)
        tools.clicked.connect(self.tools_clicked)
        b.addWidget(tools)
        settings = QPushButton(tr("   설정"))
        settings.setObjectName("NavSquare")
        settings.setCheckable(True)
        self.settings_button = settings
        settings.setIcon(theme.icon("gear-six", color=theme.TEXT))
        settings.setIconSize(QSize(20, 20))
        settings.setToolTip(tr("언어 · 보기 · 백업 보관 개수"))
        settings.setCursor(Qt.PointingHandCursor)
        settings.clicked.connect(self.settings_clicked)
        b.addWidget(settings)
        b.addSpacing(8)
        status_box = QFrame()
        status_box.setObjectName("UsbStatus")
        status = QVBoxLayout(status_box)
        status.setContentsMargins(22, 14, 22, 16)
        status.setSpacing(6)
        b.addWidget(status_box)
        self.usb_state = label("", None)
        self.usb_state.setStyleSheet("font-size: 14px;")
        status.addWidget(self.usb_state)
        self.disk_bar = QProgressBar()
        self.disk_bar.setObjectName("Disk")
        self.disk_bar.setFixedHeight(6)
        status.addWidget(self.disk_bar)
        self.disk_text = label("", "Faint")
        status.addWidget(self.disk_text)
        lay.addWidget(bottom)

        session.scan_changed.connect(self.rebuild_tree)
        session.settings_changed.connect(self._update_pending)
        session.usb_changed.connect(self.update_usb)
        self.update_usb(True)

    def rebuild_tree(self) -> None:
        expanded = {idx.data(REL_ROLE) for idx in self._all_indexes() if self.tree.isExpanded(idx)}
        current = self.tree.currentIndex().data(REL_ROLE)
        self.model.clear()
        items = {"": self.model.invisibleRootItem()}
        folder_icon = theme.icon("folder-simple", color=theme.MUTED)
        hidden_icon = theme.icon("eye-slash", color=theme.FAINT)
        hidden = self.session.hidden_folders
        for rel in sorted(self.session.scan.dirs, key=lambda r: [p.lower() for p in r.split("/")]):
            parent = rel.rpartition("/")[0]
            if parent not in items:
                continue
            item = QStandardItem(hidden_icon if rel in hidden else folder_icon, rel.rpartition("/")[2])
            item.setData(rel, REL_ROLE)
            items[parent].appendRow(item)
            items[rel] = item
        for idx in self._all_indexes():
            rel = idx.data(REL_ROLE)
            if rel in expanded or (not expanded and "/" not in rel):
                self.tree.setExpanded(idx, True)
            if rel == current:
                self.tree.setCurrentIndex(idx)
        self._update_pending()

    def _all_indexes(self):
        stack = [self.model.index(r, 0) for r in range(self.model.rowCount())]
        while stack:
            idx = stack.pop()
            yield idx
            stack.extend(self.model.index(r, 0, idx) for r in range(self.model.rowCount(idx)))

    def select_folder(self, rel: str) -> None:
        for idx in self._all_indexes():
            if idx.data(REL_ROLE) == rel:
                self.tree.setCurrentIndex(idx)
                return
        self.tree.clearSelection()
        self.tree.setCurrentIndex(QModelIndex())

    def set_tools_active(self, active: bool) -> None:
        self.set_page_active("tools" if active else None)

    def set_page_active(self, page: str | None) -> None:
        self.tools_button.setChecked(page == "tools")
        self.trash_button.setChecked(page == "trash")
        self.settings_button.setChecked(page == "settings")
        active = page is not None
        if active:
            checked = self.kind_group.checkedButton()
            if checked is not None:
                self._kind_before_tools = checked
                self.kind_group.setExclusive(False)
                checked.setChecked(False)
                self.kind_group.setExclusive(True)
        elif self.kind_group.checkedButton() is None:
            getattr(self, "_kind_before_tools", self.kind_buttons[None]).setChecked(True)

    def select_kind(self, kind) -> None:
        self.kind_buttons[kind].setChecked(True)

    def _update_pending(self) -> None:
        self.pending = self.session.pending
        self.tree.viewport().update()

    def update_usb(self, present: bool) -> None:
        dot = theme.OK if present else theme.DANGER
        name = self.session.drive.name
        text = tr("{name} 연결됨", name=name) if present else tr("{name} 분리됨", name=name)
        self.usb_state.setText(f'<span style="color:{dot}">●</span>&nbsp;&nbsp;{text}')
        if present:
            try:
                usage = shutil.disk_usage(self.session.root)
                self.disk_bar.setMaximum(1000)
                self.disk_bar.setValue(int(usage.used / usage.total * 1000))
                self.disk_text.setText(tr("디스크 용량: {size} 중 {size2} 사용 중", size=human(usage.total), size2=human(usage.used)))
            except OSError:
                self.disk_text.setText("")
        else:
            self.disk_bar.setValue(0)
            self.disk_text.setText("")
