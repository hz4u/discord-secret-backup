import threading

from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QProgressBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ...core.sync_engine import SyncEngine
from ...core.verify import verify_backup
from ...i18n import tr
from .. import theme
from ..messages import friendly
from ..right_panel import fmt_duration
from ..sidebar import human
from ..widgets import ask, button, label
from ..worker import run_async


class VerifyPanel(QWidget):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self.cancel: threading.Event | None = None
        self.result = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(label(tr("디스코드의 백업이 지금도 온전히 복원되는지 복원하기 전에 확인합니다. 내려받은 내용은 디스크에 쓰지 않습니다."), "Muted", wrap=True))

        row = QHBoxLayout()
        self.quick = button(tr("빠른 점검"), "Primary", "magnifying-glass", theme.ACCENT_INK)
        self.quick.clicked.connect(lambda: self.start(deep=False))
        row.addWidget(self.quick)
        self.deep = button(tr("정밀 점검"), icon="shield-check", icon_color=theme.TEXT)
        self.deep.clicked.connect(lambda: self.start(deep=True))
        row.addWidget(self.deep)
        row.addStretch()
        self.stop = button(tr("중지"))
        self.stop.clicked.connect(self._stop)
        self.stop.hide()
        row.addWidget(self.stop)
        lay.addLayout(row)
        self.hint = label("", "Faint", wrap=True)
        lay.addWidget(self.hint)

        self.bar = QProgressBar()
        self.bar.setMaximum(1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.status = label("", "Muted", wrap=True)
        lay.addWidget(self.status)

        self.problems = QTreeWidget()
        self.problems.setObjectName("List")
        self.problems.setHeaderLabels([tr("파일"), tr("문제")])
        self.problems.setRootIsDecorated(False)
        self.problems.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.problems.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.problems.hide()
        lay.addWidget(self.problems, 1)

        fix = QHBoxLayout()
        self.reupload = button(tr("다시 올리기"), "Primary", "upload-simple", theme.ACCENT_INK)
        self.reupload.clicked.connect(self._reupload)
        self.reupload.hide()
        fix.addWidget(self.reupload)
        self.republish = button(tr("목차 다시 올리기"), icon="list-bullets", icon_color=theme.TEXT)
        self.republish.clicked.connect(self._republish)
        self.republish.hide()
        fix.addWidget(self.republish)
        fix.addStretch()
        lay.addLayout(fix)
        self.fix_note = label("", "Faint", wrap=True)
        lay.addWidget(self.fix_note)
        lay.addStretch()

        session.settings_changed.connect(self._update)
        self._update()

    def _backup_bytes(self) -> int:
        files = (self.session.settings.manifest or {}).get("files", {}) if self.session.settings else {}
        return sum(e.get("size", 0) for e in files.values())

    def _update(self) -> None:
        running = self.cancel is not None
        ready = self.session.connected and not running
        self.quick.setEnabled(ready)
        self.deep.setEnabled(ready)
        size = self._backup_bytes()
        self.hint.setText(
            tr("빠른 점검: 모든 조각이 디스코드에 남아 있는지, 크기가 맞는지 봅니다 (몇 초~1분).\n정밀 점검: 전부 내려받아 복호화하고 지문까지 대조합니다 (복원만큼 걸림{v1}).", v1=tr(", 약 {size}", size=human(size)) if size else '')
        )
        if not self.session.connected and not running:
            self._say(tr("디스코드에 연결하면 점검할 수 있습니다."), "Muted")

    def _say(self, text: str, level: str = "Muted") -> None:
        self.status.setObjectName(level)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setText(text)

    def start(self, deep: bool) -> None:
        s = self.session
        if s.sync_running:
            self._say(tr("동기화가 끝난 뒤에 점검하세요."), "WarnText")
            return
        if deep and not ask(self, tr("정밀 점검"), tr("백업 전체({size})를 내려받아 확인합니다.\n디스크에는 쓰지 않습니다. 계속할까요?", size=human(self._backup_bytes())), yes=tr("점검 시작")):
            return
        token, guild, password, dk = s.settings.token, s.settings.guild_id, s.store.password, s.settings.dk
        self.cancel = threading.Event()
        cancel = self.cancel
        self._update()
        self.stop.show()
        self.stop.setEnabled(True)
        self.bar.setValue(0)
        self.bar.show()
        self.problems.clear()
        self.problems.hide()
        self.reupload.hide()
        self.republish.hide()
        self.fix_note.setText("")
        self._say(tr("목차를 확인하는 중..."))
        s.log(tr("정밀 점검하는 중") if deep else tr("빠른 점검하는 중"), "busy")

        def work(progress):
            client = s.client_factory(token)
            try:
                return verify_backup(client, guild, password, dk, deep=deep, on_progress=progress, cancel=cancel)
            finally:
                client.close()

        def on_progress(p):
            if p.total_files:
                self.bar.setValue(int(p.done_files / p.total_files * 1000))
            name = p.current.rpartition("/")[2]
            extra = tr(" · {size}/s · 남은 시간 {v1}", size=human(p.speed), v1=fmt_duration(p.eta)) if deep and p.speed else ""
            self._say(tr("{done_files}/{total_files}개{extra}\n{name}", done_files=p.done_files, total_files=p.total_files, extra=extra, name=name))

        run_async(work, self._done, self._failed, on_progress)

    def _stop(self) -> None:
        if self.cancel:
            self.cancel.set()
            self.stop.setEnabled(False)
            self._say(tr("멈추는 중..."))

    def _finish(self) -> None:
        self.cancel = None
        self.stop.hide()
        self._update()

    def _failed(self, exc) -> None:
        self._finish()
        self.bar.hide()
        self._say(tr("점검하지 못했습니다: {v1}", v1=friendly(exc)), "ErrorText")
        self.session.log(tr("백업을 점검하지 못했습니다: {v1}", v1=friendly(exc)), "error")

    def _done(self, r) -> None:
        self.result = r
        self._finish()
        self.bar.setValue(1000)
        kind = tr("정밀 점검") if r.deep else tr("빠른 점검")
        lines = []
        if r.aborted:
            lines.append(tr("인터넷 연결 문제로 멈췄습니다. 다시 점검하세요."))
        elif r.cancelled:
            lines.append(tr("중간에 멈췄습니다 ({v1}/{total}개 확인).", v1=r.ok + len(r.problems), total=r.total))
        if r.problems:
            lines.append(tr("문제 있는 파일 {n}개 · 정상 {ok}개", n=len(r.problems), ok=r.ok))
        elif not (r.aborted or r.cancelled):
            lines.append(tr("✓ 파일 {total}개 모두 정상", total=r.total))
        if not r.latest_index_ok:
            lines.append(tr("최신 목차가 손상되어 직전 목차로 점검했습니다."))
        if not r.password_opens_index:
            lines.append(tr("디스코드 목차가 지금 비밀번호로 열리지 않습니다. 디스코드만으로 복원하려면 옛 비밀번호가 필요합니다."))
        if r.previous_index_ok is False:
            lines.append(tr("직전 목차(예비)가 손상되어 있습니다. 다음 동기화 때 새로 채워집니다."))
        level = "OkText" if r.healthy else ("WarnText" if r.cancelled or r.aborted else "ErrorText")
        self._say("\n".join(lines), level)
        self.session.log(tr("{kind} 결과: {v1}", kind=kind, v1=lines[0].removeprefix('✓ ')), {"OkText": "info", "WarnText": "warn"}.get(level, "error"))

        if r.problems:
            for p in r.problems:
                QTreeWidgetItem(self.problems, [p.rel, p.reason])
            self.problems.show()
            here = [p.rel for p in r.problems if p.rel in self.session.scan.files]
            self.reupload.setText(tr("다시 올리기 ({n}개)", n=len(here)))
            self.reupload.setEnabled(bool(here))
            self.reupload.show()
            gone = len(r.problems) - len(here)
            note = tr("다음 [지금 동기화] 때 새로 올리고, 망가진 옛 메시지는 지웁니다.")
            if gone:
                note += tr("\n{gone}개는 이 저장소에 없어 다시 올릴 수 없습니다 (그 파일이 있는 저장소에서 점검하세요).", gone=gone)
            self.fix_note.setText(note)
        if not r.latest_index_ok or not r.password_opens_index:
            self.republish.show()
            self.republish.setEnabled(True)

    def _reupload(self) -> None:
        s = self.session
        here = [p.rel for p in self.result.problems if p.rel in s.scan.files]
        s.settings.reupload = sorted(set(s.settings.reupload) | set(here))
        s.store.save()
        s.settings_saved()
        s.log(tr("파일 {n}개를 다음 동기화 때 다시 올립니다", n=len(here)))
        self.reupload.setEnabled(False)
        self.fix_note.setText(tr("✓ {n}개를 다음 [지금 동기화] 때 다시 올립니다.", n=len(here)))

    def _republish(self) -> None:
        s = self.session
        token = s.settings.token
        self.republish.setEnabled(False)

        def work():
            client = s.client_factory(token)
            try:
                return SyncEngine(s.root, client, s.store).republish_index()
            finally:
                client.close()

        def done(ok):
            self.fix_note.setText(tr("✓ 목차를 지금 비밀번호로 다시 올렸습니다.") if ok else tr("디스코드에 백업이 없어 올리지 못했습니다."))
            s.log(tr("목차를 다시 올렸습니다") if ok else tr("목차를 다시 올리지 못했습니다"), "info" if ok else "warn")

        def failed(exc):
            self.republish.setEnabled(True)
            self.fix_note.setText(tr("목차를 올리지 못했습니다: {v1}", v1=friendly(exc)))

        run_async(work, done, failed)
