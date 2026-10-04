import threading
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QProgressBar

from ...core.restore import restore, target_has_files
from ...i18n import tr
from ..messages import friendly
from ..right_panel import fmt_duration
from ..sidebar import human
from ..widgets import Dialog, PasswordField, ask, button, joined, label
from ..worker import run_async


class RestoreDialog(Dialog):
    def __init__(self, parent, session):
        super().__init__(parent, 580)
        self.session = session
        self.cancel_event = threading.Event()
        self.restored_into: Path | None = None
        self.password_used = ""
        self.heading(tr("디스코드에서 복원"), tr("디스코드의 암호화 백업을 받아 원래 폴더 구조 그대로 되살립니다."), "clock-counter-clockwise")

        st = session.settings
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.token = PasswordField(tr("봇 토큰"))
        self.guild = QLineEdit()
        self.guild.setPlaceholderText(tr("서버 ID"))
        self.password = PasswordField(tr("메인 비밀번호"))
        if st and st.connected:
            self.token.edit.setText(st.token)
            self.guild.setText(st.guild_id)
        if session.store:
            self.password.edit.setText(session.store.password)
        self.target = QLineEdit(str(session.root))
        change = button(tr("바꾸기"))
        change.clicked.connect(self._choose)
        target_row = joined(self.target, change)
        form.addRow(label(tr("봇 토큰"), "SectionLabel"), self.token)
        form.addRow(label(tr("서버 ID"), "SectionLabel"), self.guild)
        form.addRow(label(tr("메인 비밀번호"), "SectionLabel"), self.password)
        form.addRow(label(tr("복원 위치"), "SectionLabel"), target_row)
        self.body.addLayout(form)
        self.body.addWidget(label(tr("이미 받은 파일은 건너뛰고, 이름만 같은 다른 파일은 덮어쓰지 않고 '이름 (1)'로 저장합니다."),
                                  "Faint", wrap=True))

        self.bar = QProgressBar()
        self.bar.setMaximum(1000)
        self.bar.hide()
        self.status = label("", "Muted", wrap=True)
        self.body.addWidget(self.bar)
        self.body.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch()
        self.close_button = button(tr("닫기"))
        self.close_button.clicked.connect(self._close)
        self.start = button(tr("복원 시작"), "Primary")
        self.start.clicked.connect(self._start)
        row.addWidget(self.close_button)
        row.addWidget(self.start)
        self.body.addLayout(row)

    def _choose(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("복원할 위치"), self.target.text())
        if folder:
            self.target.setText(folder)

    def _close(self) -> None:
        if self.start.text() == tr("복원 중..."):
            if ask(self, tr("복원 중지"), tr("복원을 멈출까요? 받은 파일은 그대로 남습니다."), yes=tr("멈추기"), danger=True):
                self.cancel_event.set()
            return
        self.accept() if self.restored_into else self.reject()

    def _start(self) -> None:
        token, guild, pw = self.token.text().strip(), self.guild.text().strip(), self.password.text()
        target = Path(self.target.text().strip())
        if not (token and guild and pw and self.target.text().strip()):
            self.status.setObjectName("ErrorText")
            self._restyle(self.status)
            self.status.setText(tr("모든 칸을 채우세요."))
            return
        if target_has_files(target):
            if not ask(self, tr("복원"), tr("{target} 에 이미 파일이 있습니다.\n이미 받은 파일은 건너뛰고, 이름만 같은 다른 파일은 '(1)'로 저장합니다. 계속할까요?", target=target)):
                return
        into_usb = target.resolve() == self.session.root.resolve()
        self.start.setEnabled(False)
        self.start.setText(tr("복원 중..."))
        self.bar.show()
        self.status.setObjectName("Muted")
        self._restyle(self.status)
        self.status.setText(tr("목차를 받는 중..."))
        self.session.log(tr("복원하는 중: {target}", target=target), "busy")

        def work(progress):
            client = self.session.client_factory(token)
            try:
                return restore(client, guild, pw, target, on_progress=progress, cancel=self.cancel_event,
                               write_config=into_usb and not self.session.has_config, token=token)
            finally:
                client.close()

        def on_progress(p):
            if p.total_bytes:
                self.bar.setValue(int(p.done_bytes / p.total_bytes * 1000))
            name = p.current.rpartition("/")[2]
            self.status.setText(tr("{done_files}/{total_files}개 · {size}/s · 남은 시간 {v1}\n{name}", done_files=p.done_files, total_files=p.total_files, size=human(p.speed), v1=fmt_duration(p.eta), name=name))

        def done(result):
            self.start.setText(tr("복원 시작"))
            self.restored_into = target
            self.password_used = pw
            self.bar.setValue(1000)
            lines = [tr("파일 {total}개 모두 지문 일치", total=result.total) if result.verified == result.total
                     else tr("파일 {total}개 중 {verified}개만 지문 일치", total=result.total, verified=result.verified)]
            level = "OkText"
            if result.mismatched:
                lines.append(tr("손상된 파일 {n}개 (.corrupt로 남김): ", n=len(result.mismatched)) + ", ".join(result.mismatched[:5]))
                level = "ErrorText"
            if result.failed:
                lines.append(tr("받지 못한 파일 {n}개: ", n=len(result.failed)) + ", ".join(r for r, _ in result.failed[:5]))
                level = "ErrorText"
            if result.skipped:
                lines.append(tr("이미 받아 둔 파일 {skipped}개는 건너뜀", skipped=result.skipped))
            if result.renamed:
                lines.append(tr("이름이 겹쳐 '(1)'로 저장한 파일 {n}개", n=len(result.renamed)))
            if result.used_fallback_index:
                lines.append(tr("최신 목차가 손상되어 직전 목차로 복원했습니다."))
            if result.cancelled:
                lines.insert(0, tr("중간에 멈췄습니다. 다시 복원하면 이어서 받습니다."))
                level = "WarnText"
            self.status.setObjectName(level)
            self._restyle(self.status)
            self.status.setText("\n".join(lines))
            self.session.log(tr("복원 결과: {v1}", v1=lines[0]), {"OkText": "info", "WarnText": "warn"}.get(level, "error"))
            self.close_button.setText(tr("닫기"))

        def failed(exc):
            self.start.setEnabled(True)
            self.start.setText(tr("복원 시작"))
            self.bar.hide()
            self.status.setObjectName("ErrorText")
            self._restyle(self.status)
            self.status.setText(friendly(exc))
            self.session.log(tr("복원하지 못했습니다: {v1}", v1=friendly(exc)), "error")

        run_async(work, done, failed, on_progress)

    @staticmethod
    def _restyle(w) -> None:
        w.style().unpolish(w)
        w.style().polish(w)

