from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..core import history
from ..i18n import tr
from . import theme
from .gallery import SORTS
from .widgets import Card, button, icon_label, label

CLIPBOARD_SECONDS = (15, 30, 60, 120)
VERSION_CHOICES = (10, 20, 30, 50, 100)
TRASH_CHOICES = (50, 100, 200, 500)


def _combo(items: list[str], current: int) -> QComboBox:
    box = QComboBox()
    box.addItems(items)
    box.setCurrentIndex(max(0, current))
    box.setFixedWidth(220)
    box.setFixedHeight(40)
    box.setCursor(Qt.PointingHandCursor)
    return box


class _CountCombo(QComboBox):
    def __init__(self, choices):
        super().__init__()
        self.setFixedWidth(220)
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        for n in choices:
            self.addItem(tr("{n}개", n=n), n)

    def value(self) -> int:
        return self.currentData()

    def set_value(self, n: int) -> None:
        i = self.findData(n)
        if i < 0:
            values = sorted({self.itemData(k) for k in range(self.count())} | {n})
            self.clear()
            for v in values:
                self.addItem(tr("{n}개", n=v), v)
            i = self.findData(n)
        self.setCurrentIndex(i)


class _Section(Card):
    def __init__(self, title: str):
        super().__init__()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        head = label(title, "CardTitle")
        head.setContentsMargins(20, 14, 20, 12)
        self.body.addWidget(head)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        self.body.addWidget(line)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(20, 14, 20, 16)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(4)
        self.grid.setColumnStretch(1, 1)
        self.body.addLayout(self.grid)
        self._row = 0

    def add(self, name: str, control: QWidget, hint: str = "") -> QWidget:
        if self._row:
            self.grid.setRowMinimumHeight(self._row, 12)
            self._row += 1
        self.grid.addWidget(label(name, None), self._row, 0, Qt.AlignVCenter)
        self.grid.addWidget(control, self._row, 2, Qt.AlignRight | Qt.AlignVCenter)
        self._row += 1
        if hint:
            note = label(hint, "Muted", wrap=True)
            self.grid.addWidget(note, self._row, 0, 1, 3)
            self._row += 1
        return control


class SettingsPage(QFrame):
    restart_requested = Signal()

    def __init__(self, session):
        super().__init__()
        self.setObjectName("Center")
        self.session = session
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 22, 0, 0)
        outer.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(24, 0, 24, 0)
        head.addWidget(icon_label("gear-six", theme.TEXT, 24))
        head.addSpacing(8)
        head.addWidget(label(tr("설정"), "Big"))
        head.addStretch()
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        body = QWidget()
        body.setObjectName("Center")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 12, 24, 24)
        lay.setSpacing(16)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        general = _Section(tr("일반"))
        codes = list(i18n.LANGUAGES)
        saved = session.pref("language", i18n.current_language())
        self.language = general.add(tr("언어"), _combo(list(i18n.LANGUAGES.values()), codes.index(saved) if saved in codes else 0))
        self.language.currentIndexChanged.connect(lambda i: self._set_language(codes[i]))
        self.restart_note = label(tr("다시 시작하면 바뀐 언어로 보입니다."), "WarnText")
        self.restart_btn = button(tr("지금 다시 시작"), icon="arrows-clockwise")
        self.restart_btn.clicked.connect(self.restart_requested)
        row = QHBoxLayout()
        row.addWidget(self.restart_note, 1)
        row.addWidget(self.restart_btn)
        general.grid.addLayout(row, general._row, 0, 1, 3)
        general._row += 1
        self._show_restart(saved != i18n.current_language())
        lay.addWidget(general)

        view = _Section(tr("화면"))
        self.view_mode = view.add(tr("처음 보기 방식"), _combo([tr("격자"), tr("목록")], 1 if session.pref("view", "grid") == "list" else 0))
        self.view_mode.currentIndexChanged.connect(lambda i: session.set_pref("view", "list" if i else "grid"))
        self.sort = view.add(tr("처음 정렬"), _combo([s[0] for s in SORTS], int(session.pref("sort", 0))))
        self.sort.currentIndexChanged.connect(lambda i: session.set_pref("sort", i))
        self.blur = view.add(tr("숨긴 폴더의 미리보기 흐리게"), QCheckBox(),
                             tr("숨긴 폴더 안의 사진·영상은 열기 전까지 알아볼 수 없게 흐리게 보여 줍니다."))
        self.blur.setChecked(bool(session.pref("blur_hidden", True)))
        self.blur.toggled.connect(lambda on: session.set_pref("blur_hidden", on))
        lay.addWidget(view)

        security = _Section(tr("보안"))
        seconds = int(session.pref("clipboard_seconds", 30))
        self.clipboard = security.add(
            tr("복사한 내용 자동 지우기"),
            _combo([tr("{n}초 뒤", n=n) for n in CLIPBOARD_SECONDS], CLIPBOARD_SECONDS.index(seconds) if seconds in CLIPBOARD_SECONDS else 1),
            tr("도구에서 복사한 암호문·비밀번호를 이 시간이 지나면 클립보드에서 지웁니다."))
        self.clipboard.currentIndexChanged.connect(lambda i: session.set_pref("clipboard_seconds", CLIPBOARD_SECONDS[i]))
        lay.addWidget(security)

        self.backup = _Section(tr("백업"))
        self.keep_versions = _CountCombo(VERSION_CHOICES)
        self.backup.add(tr("버전 기록 보관 개수"), self.keep_versions,
                        tr("동기화할 때마다 남는 보관함 상태를 최근 몇 개까지 둘지. 줄이면 다음 동기화 때 오래된 것부터 정리됩니다."))
        self.keep_trash = _CountCombo(TRASH_CHOICES)
        self.backup.add(tr("휴지통 보관 개수"), self.keep_trash,
                        tr("지운 파일을 최근 몇 개까지 디스코드에 남길지. 줄이면 다음 동기화 때 오래된 것부터 지워집니다."))
        self.locked_note = label(tr("잠금을 풀면 백업 설정을 바꿀 수 있습니다."), "Muted")
        lay.addWidget(self.backup)
        lay.addWidget(self.locked_note)
        lay.addStretch()

        for box in (self.keep_versions, self.keep_trash):
            box.currentIndexChanged.connect(lambda _: self._save_backup())
        session.lock_changed.connect(self._load_backup)
        self._load_backup()

    def _set_language(self, code: str) -> None:
        self.session.set_pref("language", code)
        self._show_restart(code != i18n.current_language())

    def _show_restart(self, show: bool) -> None:
        self.restart_note.setVisible(show)
        self.restart_btn.setVisible(show)

    def _load_backup(self) -> None:
        store = self.session.store
        unlocked = store is not None
        self.backup.setVisible(unlocked)
        self.locked_note.setVisible(not unlocked)
        if unlocked:
            for box, value, default in ((self.keep_versions, store.settings.keep_versions, history.KEEP_VERSIONS),
                                        (self.keep_trash, store.settings.keep_trash, history.KEEP_TRASH)):
                box.blockSignals(True)
                box.set_value(value or default)
                box.blockSignals(False)

    def _save_backup(self) -> None:
        store = self.session.store
        if store is None:
            return
        store.settings.keep_versions = self.keep_versions.value()
        store.settings.keep_trash = self.keep_trash.value()
        store.save()
