import ctypes
import sys
import tempfile
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QFontDatabase, QIcon, QIconEngine, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QScrollBar

from .. import i18n

BG = "#101010"
PANEL = "#151515"
CARD = "#1B1B1B"
CARD_HI = "#232323"
BORDER = "#262626"
BORDER_HI = "#383838"
SELECTED = "#2B2B2B"
ACCENT = "#E8E8E8"
ACCENT_HI = "#FFFFFF"
ACCENT_INK = "#111111"
ACCENT_SOFT = "#2E2E2E"
BUTTON = "#262626"
BUTTON_HI = "#303030"
FOCUS = "#6E6E6E"
TEXT = "#E6E6E6"
MUTED = "#9A9A9A"
FAINT = "#666666"
OK = "#22C55E"
WARN = "#F59E0B"
DANGER = "#EF4444"

SANS = "Pretendard"
MONO = "IBM Plex Mono"
CJK_UI = {"zh": ["Microsoft YaHei UI", "Microsoft YaHei"], "ja": ["Yu Gothic UI", "Meiryo UI", "Meiryo"]}


def sans_families() -> list[str]:
    return CJK_UI.get(i18n.current_language(), []) + [SANS]


def resource(relative: str) -> Path:
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parents[2]
    return Path(base) / relative


_FILLED: dict[str, str] = {}


def icon(name: str, color: str = MUTED, **kw) -> QIcon:
    full = _FILLED.get(name)
    if full is None:
        full = f"ph.{name}"
        if not name.endswith(("-fill", "-bold")):
            try:
                qta.icon(f"ph.{name}-fill")
                full = f"ph.{name}-fill"
            except Exception:  # noqa: BLE001
                pass
        _FILLED[name] = full
    return qta.icon(full, color=color, **kw)


class _Painted(QIconEngine):
    def pixmap(self, size, mode, state):  # noqa: N802
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        self.paint(p, QRect(QPoint(0, 0), size), mode, state)
        p.end()
        return pm


class _Centered(_Painted):
    SAMPLE = 128

    def __init__(self, icon: QIcon):
        super().__init__()
        self.icon = icon
        img = icon.pixmap(self.SAMPLE, self.SAMPLE).toImage().convertToFormat(QImage.Format_ARGB32)
        sx = sy = total = 0
        for y in range(img.height()):
            for x in range(img.width()):
                a = img.pixelColor(x, y).alpha()
                if a:
                    sx += x * a
                    sy += y * a
                    total += a
        mid = (self.SAMPLE - 1) / 2
        self.dx = (mid - sx / total) / self.SAMPLE if total else 0.0
        self.dy = (mid - sy / total) / self.SAMPLE if total else 0.0

    def paint(self, painter, rect, mode, state):  # noqa: N802
        painter.save()
        painter.translate(self.dx * rect.width(), self.dy * rect.height())
        self.icon.paint(painter, rect, Qt.AlignCenter, mode, state)
        painter.restore()

    def clone(self):
        return _Centered(self.icon)


def centered(ic: QIcon) -> QIcon:
    return QIcon(_Centered(ic))


class _Svg(_Painted):
    def __init__(self, source: str, color: str, color_disabled: str):
        super().__init__()
        self.source, self.color, self.color_disabled = source, color, color_disabled
        self._renderers: dict[str, QSvgRenderer] = {}

    def _renderer(self, color: str) -> QSvgRenderer:
        r = self._renderers.get(color)
        if r is None:
            r = self._renderers[color] = QSvgRenderer(QByteArray(self.source.replace("#000000", color).encode("utf-8")))
        return r

    def paint(self, painter, rect, mode, state):  # noqa: N802
        self._renderer(self.color_disabled if mode == QIcon.Disabled else self.color).render(painter, QRectF(rect))

    def clone(self):
        return _Svg(self.source, self.color, self.color_disabled)


def svg_icon(name: str, color: str = TEXT, color_disabled: str = FAINT) -> QIcon:
    source = resource(f"assets/icons/{name}.svg").read_text(encoding="utf-8")
    return QIcon(_Svg(source, color, color_disabled))


def app_icon() -> QIcon:
    return QIcon(str(resource("assets/secret.ico")))


def mono_font(size: int = 11) -> QFont:
    f = QFont(MONO)
    f.setPixelSize(size)
    return f


def install(app: QApplication) -> None:
    for name in ("Pretendard-Regular.otf", "Pretendard-Medium.otf", "Pretendard-SemiBold.otf", "Pretendard-Bold.otf", "IBMPlexMono-Regular.ttf"):
        QFontDatabase.addApplicationFont(str(resource(f"assets/fonts/{name}")))
    font = QFont(SANS)
    font.setFamilies(sans_families())
    font.setPixelSize(14)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setStyle("Fusion")
    app._window_styler = _WindowStyler(app)
    app.installEventFilter(app._window_styler)
    app._scroll_hover = _ScrollHover(app)
    app.installEventFilter(app._scroll_hover)
    pal = app.palette()
    for role, color in (
        (pal.ColorRole.Window, BG), (pal.ColorRole.Base, CARD), (pal.ColorRole.AlternateBase, PANEL),
        (pal.ColorRole.Text, TEXT), (pal.ColorRole.WindowText, TEXT), (pal.ColorRole.Button, CARD),
        (pal.ColorRole.ButtonText, TEXT), (pal.ColorRole.Highlight, SELECTED), (pal.ColorRole.HighlightedText, TEXT),
        (pal.ColorRole.PlaceholderText, FAINT), (pal.ColorRole.ToolTipBase, CARD_HI), (pal.ColorRole.ToolTipText, TEXT),
    ):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    arrow = Path(tempfile.gettempdir()) / "secret-caret-down.png"
    icon("caret-down-fill", color=MUTED).pixmap(24, 24).save(str(arrow))
    check = Path(tempfile.gettempdir()) / "secret-check.png"
    icon("check-bold", color=ACCENT_INK).pixmap(28, 28).save(str(check))
    carets = {}
    for name in ("caret-right", "caret-down"):
        carets[name] = Path(tempfile.gettempdir()) / f"secret-{name}.png"
        icon(name, color=MUTED).pixmap(12, 12).save(str(carets[name]))
    app.setStyleSheet(
        QSS.replace("%CHECK%", check.as_posix())
        .replace("%CARET_RIGHT%", carets["caret-right"].as_posix())
        .replace("%CARET_DOWN%", carets["caret-down"].as_posix())
        + "QComboBox::down-arrow { image: url(%s); width: 12px; height: 12px; }" % arrow.as_posix()
    )
    app.setFont(font)


def hide_titlebar_icon(widget) -> None:
    if sys.platform != "win32":
        return
    try:
        class WTA_OPTIONS(ctypes.Structure):  # noqa: N801
            _fields_ = [("dwFlags", ctypes.c_uint32), ("dwMask", ctypes.c_uint32)]

        no_icon = 0x2 | 0x4
        opts = WTA_OPTIONS(no_icon, no_icon)
        ctypes.windll.uxtheme.SetWindowThemeAttribute(
            ctypes.c_void_p(int(widget.winId())), 1, ctypes.byref(opts), ctypes.sizeof(opts)
        )
    except Exception:  # noqa: BLE001
        pass

class OverlayScrollBar(QObject):
    def __init__(self, area: QAbstractScrollArea):
        super().__init__(area)
        self.area = area
        self.inner = area.verticalScrollBar()
        self.bar = QScrollBar(Qt.Vertical, area)
        self.bar.setCursor(Qt.ArrowCursor)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inner.rangeChanged.connect(self._sync_range)
        self.inner.valueChanged.connect(self.bar.setValue)
        self.bar.valueChanged.connect(self.inner.setValue)
        area.installEventFilter(self)
        area.viewport().installEventFilter(self)
        self._sync_range(self.inner.minimum(), self.inner.maximum())
        self.bar.setValue(self.inner.value())

    def _sync_range(self, low: int, high: int) -> None:
        self.bar.setRange(low, high)
        self.bar.setPageStep(self.inner.pageStep())
        self.bar.setSingleStep(self.inner.singleStep())
        self.bar.setVisible(high > low)
        self.place()

    def place(self) -> None:
        vp = self.area.viewport().geometry()
        width = self.bar.sizeHint().width()
        right = self.area.width() - self.area.frameWidth()
        self.bar.setGeometry(right - width, vp.top(), width, vp.height())
        self.bar.raise_()

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self.place()
        return False


def add_overlay_scrollbar(area: QAbstractScrollArea) -> None:
    if getattr(area, "_overlay_bar", None) is not None or area.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff:
        return
    if area.window().windowFlags() & Qt.Popup == Qt.Popup:
        return
    area._overlay_bar = OverlayScrollBar(area)


class _ScrollHover(QObject):
    def eventFilter(self, obj, event):  # noqa: N802
        kind = event.type()
        if kind == QEvent.Show and isinstance(obj, QAbstractScrollArea):
            add_overlay_scrollbar(obj)
        if kind in (QEvent.Enter, QEvent.Leave) and obj.isWidgetType():
            area = obj
            while area is not None and not isinstance(area, QAbstractScrollArea):
                area = area.parentWidget()
            if area is not None:
                inside = area.rect().contains(area.mapFromGlobal(QCursor.pos()))
                overlay = getattr(area, "_overlay_bar", None)
                bars = [area.verticalScrollBar(), area.horizontalScrollBar()] + ([overlay.bar] if overlay else [])
                for bar in bars:
                    show = inside and bar.maximum() > 0
                    if bar.property("areaHover") != show:
                        bar.setProperty("areaHover", show)
                        bar.style().unpolish(bar)
                        bar.style().polish(bar)
        return False


class _WindowStyler(QObject):
    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Show and obj.isWidgetType() and obj.windowType() in (Qt.Window, Qt.Dialog):
            dark_titlebar(obj)
            hide_titlebar_icon(obj)
        return False


def dark_titlebar(widget) -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        dark = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        r, g, b = (int(PANEL[i : i + 2], 16) for i in (1, 3, 5))
        color = ctypes.c_int(r | (g << 8) | (b << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
    except Exception:  # noqa: BLE001
        pass


QSS = f"""
* {{ outline: 0; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {CARD_HI}; color: {TEXT}; border: 1px solid {BORDER_HI}; padding: 6px 8px; border-radius: 6px; }}

#Sidebar, #RightPanel {{ background: {PANEL}; }}
#Sidebar {{ border-right: 1px solid {BORDER}; }}
#RightPanel {{ border-left: 1px solid {BORDER}; }}
#Center {{ background: {BG}; }}
#Central {{ border-top: 1px solid rgba(255, 255, 255, 0.06); }}

#UsbCard {{ background: {CARD}; border-bottom: 1px solid {BORDER}; border-right: 1px solid {BORDER}; }}
#UsbTitle {{ font-size: 16px; font-weight: 600; }}
#UsbSub, #Muted {{ color: {MUTED}; font-size: 13px; }}
#Faint {{ color: {FAINT}; font-size: 12px; }}
#SectionLabel {{ color: {MUTED}; font-size: 13px; font-weight: 600; padding: 4px 8px; }}

QPushButton#Nav {{
    text-align: left; padding: 9px 14px; min-height: 0; border: none; border-radius: 8px;
    font-size: 15px; color: {TEXT}; background: transparent;
}}
QPushButton#NavSquare {{
    text-align: left; padding: 12px 22px; min-height: 0; border: none; border-radius: 0;
    font-size: 15px; color: {TEXT}; background: transparent;
}}
QPushButton#NavSquare:hover {{ background: {CARD}; }}
QPushButton#NavSquare:checked {{ background: {SELECTED}; color: {ACCENT_HI}; font-weight: 600; }}
QPushButton#Nav:hover {{ background: {CARD}; }}
QPushButton#Nav:checked {{ background: {SELECTED}; color: {ACCENT_HI}; font-weight: 600; }}

QTreeView {{ background: transparent; border: none; font-size: 14px; }}

QTreeView::item {{ padding: 6px 4px; border-radius: 0; }}
QTreeView::item:hover, QTreeView::branch:hover {{ background: {CARD}; }}
QTreeView::item:selected, QTreeView::branch:selected {{ background: {SELECTED}; color: {ACCENT_HI}; }}
QTreeView::branch:has-children:closed {{ image: url(%CARET_RIGHT%); }}
QTreeView::branch:has-children:open {{ image: url(%CARET_DOWN%); }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 8px 10px; font-size: 13px; }}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 12px;
    selection-background-color: {ACCENT_SOFT}; font-size: 14px; lineedit-password-character: 8226;
}}
QLineEdit, QComboBox {{ min-height: 38px; max-height: 38px; padding: 0 12px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {FOCUS}; }}
QLineEdit:disabled {{ color: {FAINT}; }}

QComboBox {{ combobox-popup: 0; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER_HI}; border-radius: 8px; padding: 4px;
    selection-background-color: {SELECTED}; outline: 0;
}}
QComboBox QAbstractItemView::item {{ min-height: 32px; padding: 0 10px; border-radius: 6px; }}
QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{
    background: {SELECTED}; color: {ACCENT_HI};
}}
QComboBox:hover {{ border-color: {BORDER_HI}; background: {CARD}; }}
QComboBox:on {{ border-color: {FOCUS}; }}

QPushButton {{
    background: {BUTTON}; border: none; border-radius: 8px;
    padding: 0 16px; min-height: 40px; font-size: 14px;
}}
QPushButton:hover {{ background: {BUTTON_HI}; }}
QPushButton:disabled {{ color: {FAINT}; background: {CARD}; }}
QPushButton#Primary {{ background: {ACCENT}; color: {ACCENT_INK}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {ACCENT_HI}; }}
QPushButton#Primary:disabled {{ background: {BORDER}; color: {FAINT}; }}
QPushButton#Danger {{ color: {DANGER}; }}
QPushButton#Link {{ background: transparent; border: none; color: {MUTED}; padding: 2px 0; min-height: 0; text-align: left; }}
QPushButton#Link:hover {{ color: {ACCENT_HI}; }}
QToolButton {{ background: transparent; border: none; border-radius: 8px; padding: 6px; }}
QToolButton:hover {{ background: {CARD_HI}; }}
QToolButton:checked {{ background: {SELECTED}; border: 1px solid {BORDER_HI}; }}
QToolButton#Crumb {{ font-size: 16px; font-weight: 600; padding: 4px 6px; }}
QToolButton#Crumb:disabled {{ color: {TEXT}; }}

#Card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
#CardTitle {{ font-size: 16px; font-weight: 600; }}
#Big {{ font-size: 20px; font-weight: 700; }}
#Dialog {{ background: {BG}; }}
#ErrorText {{ color: {DANGER}; font-size: 13px; }}
#OkText {{ color: {OK}; font-size: 13px; }}
#WarnText {{ color: {WARN}; font-size: 13px; }}
#Footer {{ color: {MUTED}; font-size: 13px; border-top: 1px solid {BORDER}; padding: 12px 20px; }}
#Overlay {{ background: rgba(16, 16, 16, 235); }}
#LogView {{ background: transparent; border: none; border-radius: 0; padding: 8px; font-size: 12px; }}
#UsbStatus {{ background: {CARD}; border-top: 1px solid {BORDER}; border-right: 1px solid {BORDER}; }}

#Joined {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
#Joined QLineEdit {{ background: transparent; border: none; border-radius: 0; }}
#Joined QPushButton, #Joined QToolButton {{ background: transparent; border: none; border-radius: 0; min-height: 38px; }}
#Joined QPushButton:hover, #Joined QToolButton:hover {{ background: {BUTTON_HI}; }}
#Joined QToolButton:checked {{ background: {SELECTED}; }}
#JoinedLine {{ background: {BORDER}; }}

QProgressBar {{ background: {PANEL}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QProgressBar#Disk {{ border-radius: 0; }}
QProgressBar#Disk::chunk {{ background: {MUTED}; border-radius: 0; }}

QCheckBox {{ spacing: 8px; font-size: 14px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {BORDER_HI}; background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url(%CHECK%); }}
QTreeView::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER_HI}; background: {PANEL}; }}
QTreeView::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url(%CHECK%); }}
QTreeView::indicator:hover {{ border-color: {FAINT}; }}

QTabWidget::pane {{ border: none; }}
QTabWidget#ToolTabs::tab-bar {{ left: 24px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 10px 16px; border-bottom: 2px solid transparent; font-size: 14px; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {ACCENT}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}

QScrollBar::handle:vertical {{ background: transparent; border-radius: 4px; min-height: 30px; }}
QScrollBar[areaHover="true"]::handle:vertical {{ background: {BORDER_HI}; }}
QScrollBar[areaHover="true"]::handle:vertical:hover {{ background: {FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; height: 0; }}
QScrollBar:horizontal {{ height: 0; }}

QListView#Grid, QTreeView#List {{ background: transparent; border: none; }}

QTreeView#List::item {{ padding: 8px 6px 8px 24px; }}
QTreeView#List QHeaderView::section {{ padding: 8px 6px 8px 24px; border-top: 1px solid {BORDER}; }}
QListWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; font-size: 13px; padding: 4px; }}
QMenu {{ background: {CARD}; border: 1px solid {BORDER_HI}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 8px 18px 8px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {SELECTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QMessageBox {{ background: {BG}; }}

#DirtyMark {{ color: {MUTED}; font-size: 12px; padding-left: 6px; }}
QPlainTextEdit#Editor {{ background: {PANEL}; padding: 14px 16px; font-size: 14px; }}
QGraphicsView#ImageView {{ background: {BG}; border: none; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER_HI}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; background: {ACCENT_HI}; }}
"""
