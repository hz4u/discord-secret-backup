import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QPlainTextEdit, QStackedWidget, QTabWidget, QVBoxLayout, QWidget

from ...core.crypto_core import DISCORD_LIMIT, decrypt_text, encrypt_text
from ...core.file_crypto import DISCORD_FILE_LIMIT, decrypt_file, encrypt_file, is_encrypted_file
from ...i18n import tr
from .. import theme
from ..messages import friendly
from ..sidebar import human
from ..widgets import ProgressButton, button, icon_label, label, set_icon
from ..worker import run_async
from .change_password import ChangePasswordPanel
from .duplicates import DuplicatesPanel
from .hide_folders import HideFoldersPanel
from .timeline import VersionsPanel
from .verify import VerifyPanel


def _mono_box(readonly: bool = False, height: int = 110) -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setFont(theme.mono_font(13))
    box.setReadOnly(readonly)
    box.setMinimumHeight(height)
    return box


class _TextTool(QWidget):
    BOX_HEIGHT = 120

    def __init__(self, tools, *, in_title, in_hint, in_mono, action, action_icon, out_title, out_hint, out_mono,
                 reveal=False):
        super().__init__()
        self.tools = tools
        self.action = action
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 0)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label(in_title, "SectionLabel"))
        head.addStretch()
        self.error = label("", "ErrorText")
        head.addWidget(self.error)
        self.go = button(action, "Primary", action_icon, theme.ACCENT_INK)
        self.go.setEnabled(False)
        self.go.clicked.connect(self._run)
        head.addWidget(self.go)
        lay.addLayout(head)
        self.input = _mono_box(height=self.BOX_HEIGHT) if in_mono else QPlainTextEdit()
        self.input.setMinimumHeight(self.BOX_HEIGHT)
        self.input.setPlaceholderText(in_hint)
        self.input.textChanged.connect(lambda: self.go.setEnabled(bool(self.input.toPlainText().strip())))
        lay.addWidget(self.input, 1)
        lay.addSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label(out_title, "SectionLabel"))
        head.addStretch()
        self.reveal = None
        if reveal:
            self.reveal = button(tr("보기"), icon="eye", icon_color=theme.TEXT)
            self.reveal.setEnabled(False)
            self.reveal.clicked.connect(self._toggle)
            head.addWidget(self.reveal)
        self.copy = button(tr("복사"), icon="copy", icon_color=theme.TEXT)
        self.copy.setEnabled(False)
        head.addWidget(self.copy)
        lay.addLayout(head)
        self.output = _mono_box(readonly=True, height=self.BOX_HEIGHT) if out_mono else QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(self.BOX_HEIGHT)
        self.output.setPlaceholderText(out_hint)
        lay.addWidget(self.output, 1)
        self.count = label("", "Muted")
        self.count.setFixedHeight(20)
        lay.addWidget(self.count)

    def _toggle(self) -> None:
        pass

    def _run(self) -> None:
        raise NotImplementedError


class _TextEncrypt(_TextTool):
    def __init__(self, tools):
        super().__init__(tools, in_title=tr("내용"), in_hint=tr("암호화할 비밀번호, 복구키, 메모…"), in_mono=False,
                         action=tr("암호화"), action_icon="lock-simple",
                         out_title=tr("암호문 (디스코드에 붙여넣기)"), out_hint=tr("암호화하면 여기에 ENC1 암호문이 나타납니다."),
                         out_mono=True)
        self.copy.clicked.connect(lambda: self.tools.copy(self.output.toPlainText()))

    def _run(self) -> None:
        text, pw = self.input.toPlainText(), self.tools.password
        self.go.setEnabled(False)
        self.go.setText(tr("암호화 중..."))
        self.error.setText("")

        def done(token):
            self.go.setText(tr("암호화"))
            self.go.setEnabled(True)
            self.output.setPlainText(token)
            n = len(token)
            over = n > DISCORD_LIMIT
            self.count.setText(tr("{n:,} / {DISCORD_LIMIT:,}자", n=n, DISCORD_LIMIT=DISCORD_LIMIT) + (tr(" · 디스코드 한 메시지에 안 들어갑니다") if over else ""))
            self.count.setStyleSheet(f"color: {theme.DANGER if over else theme.MUTED};")
            self.copy.setEnabled(True)

        def failed(exc):
            self.go.setText(tr("암호화"))
            self.go.setEnabled(True)
            self.error.setText(friendly(exc))

        run_async(lambda: encrypt_text(text, pw), done, failed)


class _TextDecrypt(_TextTool):
    def __init__(self, tools):
        super().__init__(tools, in_title=tr("암호문"), in_hint=tr("디스코드에서 복사한 ENC1:… 을 붙여넣으세요. 코드블록 기호가 섞여 있어도 됩니다."),
                         in_mono=True, action=tr("복호화"), action_icon="lock-key-open",
                         out_title=tr("풀린 내용"), out_hint=tr("복호화하면 가려진 채로 나타납니다. [보기]를 눌러야 보입니다."),
                         out_mono=False, reveal=True)
        self.value = ""
        self.shown = False
        self.copy.clicked.connect(lambda: self.tools.copy(self.value))

    def _render(self) -> None:
        self.output.setPlainText(self.value if self.shown else "•" * min(len(self.value), 30))
        self.reveal.setText(tr("숨기기") if self.shown else tr("보기"))
        set_icon(self.reveal, "eye-slash" if self.shown else "eye")

    def _toggle(self) -> None:
        self.shown = not self.shown
        self._render()

    def _run(self) -> None:
        token, pw = self.input.toPlainText(), self.tools.password
        self.go.setEnabled(False)
        self.go.setText(tr("복호화 중..."))
        self.error.setText("")

        def done(text):
            self.go.setText(tr("복호화"))
            self.go.setEnabled(True)
            self.value, self.shown = text, False
            self._render()
            self.reveal.setEnabled(True)
            self.copy.setEnabled(True)

        def failed(exc):
            self.go.setText(tr("복호화"))
            self.go.setEnabled(True)
            self.value = ""
            self.output.clear()
            self.reveal.setEnabled(False)
            self.copy.setEnabled(False)
            self.error.setText(friendly(exc))

        run_async(lambda: decrypt_text(token, pw), done, failed)


class _DropArea(QFrame):
    def __init__(self, on_file):
        super().__init__()
        self.on_file = on_file
        self.setAcceptDrops(True)
        self.setMinimumHeight(240)
        self._style(False)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(10)
        pic = label()
        pic.setPixmap(theme.icon("upload-simple", color=theme.MUTED).pixmap(36, 36))
        pic.setAlignment(Qt.AlignCenter)
        lay.addWidget(pic)
        hint = label(tr("파일을 여기로 끌어오거나"), "Muted")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        choose = button(tr("파일 선택"), icon="file", icon_color=theme.TEXT)
        choose.clicked.connect(self._choose)
        lay.addWidget(choose, 0, Qt.AlignCenter)
        self.name = label("", "CardTitle")
        self.name.setAlignment(Qt.AlignCenter)
        self.name.hide()
        lay.addSpacing(4)
        lay.addWidget(self.name)
        self.mode = label("", "Muted")
        self.mode.setAlignment(Qt.AlignCenter)
        self.mode.hide()
        lay.addWidget(self.mode)

    def show_file(self, name: str, mode: str) -> None:
        self.name.setText(name)
        self.mode.setText(mode)
        self.name.show()
        self.mode.show()

    def _style(self, hover: bool) -> None:
        color = theme.ACCENT if hover else theme.BORDER_HI
        self.setStyleSheet(f"_DropArea {{ border: 2px dashed {color}; border-radius: 12px; background: {theme.PANEL}; }}")

    def _choose(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, tr("파일 선택"))
        if name:
            self.on_file(Path(name))

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, e):  # noqa: N802
        self._style(False)

    def dropEvent(self, e):  # noqa: N802
        self._style(False)
        urls = [u for u in e.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.on_file(Path(urls[0].toLocalFile()))


class _FileCrypt(QWidget):
    def __init__(self, tools):
        super().__init__()
        self.tools = tools
        self.path: Path | None = None
        self.result: Path | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 0)
        lay.setSpacing(8)
        self.drop = _DropArea(self._set)
        lay.addWidget(self.drop)
        self.go = ProgressButton(tr("암호화"))
        self.go.setCursor(Qt.PointingHandCursor)
        set_icon(self.go, "lock-simple", theme.ACCENT_INK)
        self.go.setEnabled(False)
        self.go.clicked.connect(self._run)
        lay.addWidget(self.go)
        lay.addSpacing(4)
        self.status = label("", "Muted", wrap=True)
        lay.addWidget(self.status)
        self.open = button(tr("폴더 열기"), icon="folder-open", icon_color=theme.TEXT)
        self.open.hide()
        self.open.clicked.connect(lambda: subprocess.Popen(["explorer", "/select,", str(self.result)]))
        lay.addWidget(self.open, 0, Qt.AlignLeft)
        lay.addStretch()

    def _label(self, dec: bool) -> None:
        self.go.setText(tr("복호화") if dec else tr("암호화"))
        set_icon(self.go, "lock-key-open" if dec else "lock-simple", theme.ACCENT_INK)

    def _set(self, path: Path) -> None:
        if not path.is_file():
            self.status.setText(tr("폴더는 넣을 수 없습니다. 파일을 하나 고르세요."))
            return
        self.path = path
        dec = is_encrypted_file(path)
        self.drop.show_file(f"{path.name}   {human(path.stat().st_size)}", tr("암호화된 파일 → 복호화") if dec else tr("일반 파일 → 암호화"))
        self._label(dec)
        self.go.setEnabled(True)
        self.status.setText("")
        self.open.hide()

    def _run(self) -> None:
        src, pw = self.path, self.tools.password
        dec = is_encrypted_file(src)
        self.go.setEnabled(False)
        self.go.set_progress(0.0)
        self.go.setText(tr("복호화 중... {p}%", p=0) if dec else tr("암호화 중... {p}%", p=0))

        def progress(frac: float) -> None:
            self.go.set_progress(frac)
            p = int(frac * 100)
            self.go.setText(tr("복호화 중... {p}%", p=p) if dec else tr("암호화 중... {p}%", p=p))

        def finish() -> None:
            self.go.set_progress(None)
            self._label(dec)
            self.go.setEnabled(True)

        def done(out: Path):
            self.result = out
            finish()
            if dec:
                self.status.setText(tr("✓ 원래 이름으로 복원했습니다: {name}", name=out.name))
            else:
                note = tr("✓ 저장됨: {name}\n원본은 자동으로 지우지 않습니다. 필요 없으면 직접 삭제하세요.", name=out.name)
                if out.stat().st_size > DISCORD_FILE_LIMIT:
                    note += tr("\n10MB를 넘어서 디스코드 무료 계정으로는 올릴 수 없습니다.")
                self.status.setText(note)
            self.status.setStyleSheet(f"color: {theme.OK};")
            self.open.show()

        def failed(exc):
            finish()
            self.status.setStyleSheet(f"color: {theme.DANGER};")
            self.status.setText(friendly(exc))

        work = (lambda report: decrypt_file(src, pw, report)) if dec else (lambda report: encrypt_file(src, pw, report))
        run_async(work, done, failed, progress)


PAD = 24


class ToolsPage(QFrame):
    def __init__(self, session, clipboard, request_unlock, request_create, request_sync=None):
        super().__init__()
        self.setObjectName("Center")
        self.session = session
        self.clipboard = clipboard
        self.request_sync = request_sync
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 22, 0, 16)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(PAD, 0, PAD, 0)
        head.addWidget(icon_label("wrench", theme.TEXT, 24))
        head.addSpacing(8)
        head.addWidget(label(tr("도구"), "Big"))
        head.addStretch()
        lay.addLayout(head)
        intro = label(tr("메인 비밀번호로 동작하는 도구입니다: 텍스트·파일 암호화, 폴더 숨기기, 중복 파일, 백업 점검, 버전 기록, 비밀번호 변경."), "Muted", wrap=True)
        intro.setContentsMargins(PAD, 0, PAD, 0)
        lay.addWidget(intro)

        self.stack = QStackedWidget()
        locked = QWidget()
        ll = QVBoxLayout(locked)
        ll.setAlignment(Qt.AlignCenter)
        self.locked_text = label("", "Muted")
        self.locked_text.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.locked_text)
        self.locked_button = button("", "Primary")
        self.locked_button.setFixedWidth(200)
        self.locked_button.clicked.connect(lambda: (request_unlock if session.has_config else request_create)())
        ll.addWidget(self.locked_button, 0, Qt.AlignCenter)
        self.stack.addWidget(locked)
        self.tabs = None
        lay.addWidget(self.stack, 1)

        self.clip = label("", "Faint")
        self.clip.setContentsMargins(PAD, 0, PAD, 0)
        lay.addWidget(self.clip)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        session.lock_changed.connect(self.update_state)
        self.update_state()

    def update_state(self) -> None:
        s = self.session
        if s.unlocked:
            if self.tabs is None:
                self.tabs = QTabWidget()
                self.tabs.setObjectName("ToolTabs")
                self.tabs.addTab(_TextEncrypt(self), tr("텍스트 암호화"))
                self.tabs.addTab(_TextDecrypt(self), tr("텍스트 복호화"))
                self.tabs.addTab(_FileCrypt(self), tr("파일 암호화"))
                self.tabs.addTab(HideFoldersPanel(s), tr("숨기기"))
                self.tabs.addTab(DuplicatesPanel(s), tr("중복 파일"))
                self.tabs.addTab(VerifyPanel(s), tr("백업 점검"))
                self.tabs.addTab(VersionsPanel(s, self.request_sync), tr("버전 기록"))
                self.tabs.addTab(ChangePasswordPanel(s), tr("비밀번호 변경"))
                for i in range(self.tabs.count()):
                    page = self.tabs.widget(i)
                    if not getattr(page, "full_width", False):
                        m = page.layout().contentsMargins()
                        page.layout().setContentsMargins(PAD, m.top(), PAD, m.bottom())
                self.stack.addWidget(self.tabs)
            self.stack.setCurrentWidget(self.tabs)
        else:
            self.stack.setCurrentIndex(0)
            if s.has_config:
                self.locked_text.setText(tr("잠금을 풀면 도구를 쓸 수 있습니다."))
                self.locked_button.setText(tr("잠금 해제"))
            else:
                self.locked_text.setText(tr("메인 비밀번호를 만들면 도구를 쓸 수 있습니다."))
                self.locked_button.setText(tr("메인 비밀번호 만들기"))

    @property
    def password(self) -> str:
        return self.session.store.password

    def copy(self, text: str) -> None:
        try:
            self.clipboard.copy(text)
        except OSError as exc:
            self.clip.setText(friendly(exc))

    def _tick(self) -> None:
        r = self.clipboard.remaining
        self.clip.setText(tr("복사한 내용은 {r}초 후 클립보드에서 지워집니다 · Windows 클립보드 기록에는 남지 않습니다", r=r) if r else "")
