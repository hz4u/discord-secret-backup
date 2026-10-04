from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QVBoxLayout, QWidget

from ...core.sync_engine import SyncEngine
from ...i18n import tr
from ..messages import friendly
from ..widgets import NewPasswordFields, PasswordField, button, confirm_weak, label
from ..worker import run_async


class ChangePasswordPanel(QWidget):
    changed = Signal(str)

    def __init__(self, session):
        super().__init__()
        self.session = session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 0)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.current = PasswordField(tr("현재 비밀번호"))
        form.addRow(label(tr("현재 비밀번호"), "SectionLabel"), self.current)
        self.new = NewPasswordFields(tr("새 비밀번호"), form=form)
        lay.addLayout(form)
        self.status = label("", "ErrorText", wrap=True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch()
        self.ok = button(tr("변경"), "Primary")
        self.ok.clicked.connect(self._change)
        row.addWidget(self.ok)
        lay.addLayout(row)
        lay.addStretch()
        self.new.changed.connect(self._update)
        self.current.changed.connect(self._update)
        self._update()

    def _update(self) -> None:
        self.ok.setEnabled(bool(self.current.text()) and self.new.valid())

    def _say(self, text: str, ok: bool = False) -> None:
        self.status.setObjectName("OkText" if ok else "ErrorText")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setText(text)

    def _change(self) -> None:
        store = self.session.store
        if self.current.text() != store.password:
            self._say(tr("현재 비밀번호가 틀렸습니다."))
            return
        if not confirm_weak(self, self.new):
            return
        self.ok.setEnabled(False)
        self.ok.setText(tr("바꾸는 중..."))
        new = self.new.text()
        session = self.session

        def work():
            store.change_password(new)
            if not session.connected:
                return False
            client = session.client_factory(store.settings.token)
            try:
                return SyncEngine(session.root, client, store).republish_index()
            finally:
                client.close()

        def finish(message: str, ok: bool) -> None:
            self.ok.setText(tr("변경"))
            self.current.clear()
            self.new.clear()
            self._say(message, ok)
            session.settings_saved()
            self.changed.emit(new)

        def failed(exc):
            if store.password == new:
                finish(tr("비밀번호는 바뀌었지만 디스코드 목차를 갱신하지 못했습니다. 다음 동기화 때 갱신됩니다. ({v1})", v1=friendly(exc)), False)
            else:
                self.ok.setText(tr("변경"))
                self._update()
                self._say(friendly(exc))

        run_async(work, lambda _: finish(tr("✓ 메인 비밀번호를 바꿨습니다."), True), failed)

