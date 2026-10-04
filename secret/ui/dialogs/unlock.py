from PySide6.QtWidgets import QHBoxLayout

from ...core.config_store import ConfigCorruptError, backup_path
from ...i18n import tr
from .. import theme
from ..messages import friendly
from ..widgets import Dialog, PasswordField, ask, button, label


class UnlockDialog(Dialog):
    def __init__(self, parent, session, reason: str = ""):
        super().__init__(parent, 440)
        self.session = session
        self.heading(tr("잠금 해제"), reason or tr("메인 비밀번호를 입력하세요. 프로그램을 끄거나 잠글 때까지 기억합니다."), "lock-key", theme.ACCENT)
        self.password = PasswordField(tr("메인 비밀번호"))
        self.password.submitted.connect(self._open)
        self.password.changed.connect(lambda: self.error.setText(""))
        self.body.addWidget(self.password)
        self.error = label("", "ErrorText", wrap=True)
        self.body.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        cancel = button(tr("취소"))
        cancel.clicked.connect(self.reject)
        self.ok = button(tr("열기"), "Primary")
        self.ok.clicked.connect(self._open)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        self.body.addLayout(row)
        self.password.setFocus()

    def _open(self, source=None) -> None:
        if not self.password.text() or not self.ok.isEnabled():
            return
        self.ok.setEnabled(False)
        self.ok.setText(tr("확인 중..."))

        def failed(exc):
            self.ok.setEnabled(True)
            self.ok.setText(tr("열기"))
            if isinstance(exc, ConfigCorruptError) and source is None and backup_path(self.session.config_path).exists():
                if ask(self, tr("설정 파일 손상"), tr("설정 파일이 손상되었습니다.\n직전 백업(config.bak)으로 열어볼까요?")):
                    self._open(backup_path(self.session.config_path))
                    return
            self.error.setText(friendly(exc))
            self.password.edit.selectAll()
            self.password.setFocus()

        self.session.unlock_async(self.password.text(), self.accept, failed, source=source)
