import base64

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QFrame, QHBoxLayout, QLineEdit, QVBoxLayout

from ...core.crypto_core import DecryptionError
from ...core.discord_api import REQUIRED_PERMISSIONS, check_connection, fetch_guild_icon
from ...core.restore import open_backup
from ...i18n import tr
from .. import theme
from ..messages import friendly
from ..widgets import Dialog, NewPasswordFields, PasswordField, button, confirm_weak, icon_label, label
from ..worker import run_async

RESTORE = 2


class ConnectDialog(Dialog):
    def __init__(self, parent, session, mode: str):
        super().__init__(parent, 560)
        self.session = session
        self.mode = mode
        self.info = None

        if mode == "password":
            self.heading(tr("메인 비밀번호 만들기"), tr("도구와 디스코드 백업이 모두 이 비밀번호 하나로 동작합니다. 잊으면 누구도 풀 수 없습니다."), "key")
        else:
            self.heading(tr("Discord 연결"), tr("봇 토큰과 서버 ID를 넣고 연결을 확인하세요."), "fa6b.discord", theme.TEXT)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        form.setVerticalSpacing(10)
        if mode != "password":
            self.token = PasswordField(tr("봇 토큰"))
            self.guild = QLineEdit()
            self.guild.setPlaceholderText(tr("예: 123456789012345678"))
            form.addRow(label(tr("봇 토큰"), "SectionLabel"), self.token)
            form.addRow(label(tr("서버 ID"), "SectionLabel"), self.guild)
            row = QHBoxLayout()
            row.addStretch()
            self.check = button(tr("연결 확인"), icon="link", icon_color=theme.TEXT)
            self.check.clicked.connect(self._check)
            row.addWidget(self.check)
            form.addRow("", row)
            self.result = QFrame()
            self.result.setObjectName("Card")
            self.result_lay = QVBoxLayout(self.result)
            self.result_lay.setContentsMargins(16, 12, 16, 12)
            self.result.hide()
            form.addRow(self.result)
            self.token.changed.connect(self._reset_check)
            self.guild.textChanged.connect(self._reset_check)

        self.passwords = None
        if mode in ("new", "password"):
            self.passwords = NewPasswordFields(form=form)
            self.passwords.changed.connect(self._update)
        self.body.addLayout(form)
        if self.passwords is not None:
            self.body.addWidget(label(tr("⚠ 이 비밀번호를 잊으면 디스코드 백업도, 도구로 만든 암호문도 누구도 풀 수 없습니다."), "WarnText", wrap=True))
        self.error = label("", "ErrorText", wrap=True)
        self.body.addWidget(self.error)
        row = QHBoxLayout()
        if mode == "new":
            restore = button(tr("기존 백업에서 복원"), "Link")
            restore.clicked.connect(lambda: self.done(RESTORE))
            row.addWidget(restore)
        row.addStretch()
        cancel = button(tr("취소"))
        cancel.clicked.connect(self.reject)
        self.save = button(tr("저장"), "Primary")
        self.save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(self.save)
        self.body.addLayout(row)
        self._update()

    def _reset_check(self) -> None:
        self.info = None
        self.result.hide()
        self._update()

    def _check(self) -> None:
        token, guild = self.token.text().strip(), self.guild.text().strip()
        if not token or not guild:
            self.error.setText(tr("봇 토큰과 서버 ID를 모두 넣으세요."))
            return
        self.error.setText("")
        self.check.setEnabled(False)
        self.check.setText(tr("확인 중..."))
        password = self.session.store.password if self.session.store else None
        dk = self.session.settings.dk if self.session.store else None

        def work():
            client = self.session.client_factory(token)
            try:
                info = check_connection(client, guild)
                same_backup = None
                if info.has_backup and password:
                    try:
                        _, backup_dk, _, _ = open_backup(client, guild, password)
                        same_backup = backup_dk == dk
                    except DecryptionError:
                        same_backup = False
                return info, same_backup, fetch_guild_icon(client, guild, info.icon)
            finally:
                client.close()

        run_async(work, self._checked, self._check_failed)

    def _check_failed(self, exc) -> None:
        self.check.setEnabled(True)
        self.check.setText(tr("연결 확인"))
        self.error.setText(friendly(exc))

    def _checked(self, value) -> None:
        info, same_backup, icon_bytes = value
        self.icon_bytes = icon_bytes
        self.check.setEnabled(True)
        self.check.setText(tr("연결 확인"))
        while self.result_lay.count():
            w = self.result_lay.takeAt(0)
            if w.widget():
                w.widget().deleteLater()
            elif w.layout():
                while w.layout().count():
                    w.layout().takeAt(0).widget().deleteLater()
        self.result_lay.addWidget(label(tr("봇  {bot_name}   ·   서버  {guild_name}", bot_name=info.bot_name, guild_name=info.guild_name), "CardTitle"))
        for name in REQUIRED_PERMISSIONS:
            ok = name not in info.missing
            row = QHBoxLayout()
            row.addWidget(icon_label("check-circle" if ok else "x-circle", theme.OK if ok else theme.DANGER, 16))
            row.addWidget(label(name, None if ok else "ErrorText"))
            row.addStretch()
            self.result_lay.addLayout(row)
        self.blocker = ""
        if info.missing:
            self.blocker = tr("봇 역할에 빠진 권한을 켜고 다시 확인하세요. (서버 설정 → 역할)")
        elif info.has_backup and self.mode == "new":
            self.blocker = tr("이 서버에는 이미 Secret 백업이 있습니다. 아래 '기존 백업에서 복원'을 쓰세요.")
        elif info.has_backup and same_backup is False:
            self.blocker = tr("이 서버에는 다른 비밀번호로 만든 백업이 있습니다. 다른 서버를 쓰거나 ⚙ → 디스코드에서 복원을 쓰세요.")
        if self.blocker:
            self.result_lay.addWidget(label(self.blocker, "ErrorText", wrap=True))
        elif info.has_backup:
            self.result_lay.addWidget(label(tr("이 서버의 기존 백업에 이어서 동기화합니다."), "OkText"))
        else:
            self.result_lay.addWidget(label(tr("연결할 수 있습니다."), "OkText"))
        self.info = info
        self.result.show()
        self._update()

    def _update(self) -> None:
        ok = True
        if self.mode != "password":
            ok = self.info is not None and not getattr(self, "blocker", "")
        if self.passwords is not None:
            ok = ok and self.passwords.valid()
        self.save.setEnabled(ok)

    def _save(self) -> None:
        if self.passwords is not None and not confirm_weak(self, self.passwords):
            return
        conn = {}
        if self.mode != "password":
            conn = dict(
                token=self.token.text().strip(), guild_id=self.guild.text().strip(),
                guild_name=self.info.guild_name, bot_name=self.info.bot_name,
                guild_icon=base64.b64encode(self.icon_bytes).decode() if self.icon_bytes else "",
                guild_icon_hash=self.info.icon or "",
            )
        if self.mode == "attach":
            st = self.session.settings
            for k, v in conn.items():
                setattr(st, k, v)
            try:
                self.session.store.save()
            except OSError as exc:
                self.error.setText(friendly(exc))
                return
            self.session.settings_saved()
            self.accept()
            return
        self.save.setEnabled(False)
        self.save.setText(tr("저장 중..."))

        def failed(exc):
            self.save.setText(tr("저장"))
            self._update()
            self.error.setText(friendly(exc))

        self.session.create_async(self.passwords.text(), self.accept, failed, **conn)
