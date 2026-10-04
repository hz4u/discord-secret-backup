from datetime import datetime

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from ...core.sync_engine import SyncEngine
from ...core.timeline import apply_rollback, plan_rollback
from ...i18n import tr
from .. import theme
from ..messages import counts_text, friendly
from ..sidebar import human
from ..widgets import ask, button, label
from ..worker import run_async

KIND_TEXT = {"sync": tr("동기화"), "rollback": tr("되돌리기"), "trash-restore": tr("휴지통 복구"), "trash-purge": tr("휴지통 비우기")}


def fmt_at(at: str) -> str:
    try:
        return datetime.fromisoformat(at).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return at


def summary_text(summary: dict) -> str:
    return counts_text([(tr("새로운 파일"), summary.get("new", 0)), (tr("바뀐 파일"), summary.get("changed", 0)),
                        (tr("삭제된 파일"), summary.get("deleted", 0)), (tr("이름이 바뀐 파일"), summary.get("renamed", 0))],
                       empty=tr("변화 없음"))


class _Panel(QWidget):
    full_width = True

    def __init__(self, session, intro: str):
        super().__init__()
        self.session = session
        self.engine: SyncEngine | None = None
        self.client = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 16, 0, 0)
        lay.setSpacing(10)
        pad = 24
        text = label(intro, "Muted", wrap=True)
        text.setContentsMargins(pad, 0, pad, 0)
        lay.addWidget(text)
        self.top = QHBoxLayout()
        self.top.setContentsMargins(pad, 0, pad, 0)
        self.load_btn = button(tr("불러오기"), icon="arrows-clockwise", icon_color=theme.TEXT)
        self.load_btn.clicked.connect(self.load)
        self.top.addWidget(self.load_btn)
        self.top.addStretch()
        lay.addLayout(self.top)
        self.view = QTreeWidget()
        self.view.setObjectName("List")
        self.view.setRootIsDecorated(False)
        self.view.itemSelectionChanged.connect(self._update)
        self.view.itemChanged.connect(lambda *_: self._update())
        lay.addWidget(self.view, 1)
        self.status = label("", "Muted", wrap=True)
        self.status.setContentsMargins(pad, 0, pad, 0)
        lay.addWidget(self.status)
        session.settings_changed.connect(self._update)

    def _say(self, text: str, level: str = "Muted") -> None:
        self.status.setObjectName(level)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.status.setText(text)

    def _busy(self) -> bool:
        return self.session.sync_running

    def _update(self) -> None:
        self.load_btn.setEnabled(self.session.connected and not self._busy())

    def _open_engine(self):
        s = self.session
        client = s.client_factory(s.settings.token)
        return client, SyncEngine(s.root, client, s.store)

    def load(self) -> None:
        if not self.session.connected:
            self._say(tr("디스코드에 연결하면 볼 수 있습니다."))
            return
        self._say(tr("디스코드에서 목차를 읽는 중..."))
        self.load_btn.setEnabled(False)

        def work():
            client, engine = self._open_engine()
            try:
                engine.load_manifest()
            finally:
                client.close()
            return engine

        def done(engine):
            self.engine = engine
            self.fill()
            self._update()

        def failed(exc):
            self._say(tr("불러오지 못했습니다: {v1}", v1=friendly(exc)), "ErrorText")
            self._update()

        run_async(work, done, failed)

    def fill(self) -> None:
        raise NotImplementedError

    def _begin(self) -> None:
        self.session.sync_running = True
        self._update()

    def _end(self) -> None:
        self.session.sync_running = False
        self.session.settings_saved()
        self.session.refresh_scan()
        self._update()


class VersionsPanel(_Panel):
    def __init__(self, session, request_sync=None):
        super().__init__(session, tr("동기화할 때마다 보관함 전체 상태가 버전으로 남습니다 (몇 개까지 남길지는 설정에서 정합니다). 고른 시점으로 보관함을 되돌릴 수 있고, 되돌리기도 버전으로 남아 다시 되돌릴 수 있습니다."))
        self.request_sync = request_sync
        self.view.setHeaderLabels([tr("시각"), tr("변화"), tr("종류"), tr("상태")])
        self.view.setUniformRowHeights(True)
        self.view.setIconSize(QSize(20, 20))
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, width in ((0, 220), (2, 130), (3, 130)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            header.resizeSection(col, width)
        self.rollback_btn = button(tr("이 시점으로 되돌리기"), "Primary", "clock-counter-clockwise", theme.ACCENT_INK)
        self.rollback_btn.clicked.connect(self.rollback)
        self.top.addWidget(self.rollback_btn)
        self.sync_btn = button(tr("지금 동기화"))
        self.sync_btn.clicked.connect(lambda: self.request_sync and self.request_sync())
        self.sync_btn.hide()
        self.top.insertWidget(1, self.sync_btn)
        self._update()

    def fill(self) -> None:
        self.view.clear()
        versions = list(reversed(self.engine.manifest.history))
        muted, ok = QColor(theme.MUTED), QColor(theme.OK)
        for i, v in enumerate(versions):
            item = QTreeWidgetItem([fmt_at(v.at), summary_text(v.summary), KIND_TEXT.get(v.kind, v.kind), tr("✓ 현재 버전") if i == 0 else ""])
            item.setData(0, Qt.UserRole, v.id)
            item.setIcon(0, theme.icon("clock-counter-clockwise", color=theme.MUTED))
            for col in (1, 2):
                item.setForeground(col, muted)
            item.setForeground(3, ok)
            self.view.addTopLevelItem(item)
        self._say(tr("버전 {n}개", n=len(versions)) if versions else tr("아직 기록된 버전이 없습니다. 동기화하면 첫 버전이 남습니다."))

    def _selected(self) -> int | None:
        items = self.view.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else None

    def _update(self) -> None:
        super()._update()
        vid = self._selected()
        latest = self.engine.manifest.history[-1].id if self.engine and self.engine.manifest.history else None
        self.rollback_btn.setEnabled(bool(vid) and vid != latest and self.session.connected and not self._busy())

    def rollback(self) -> None:
        vid = self._selected()
        if vid is None:
            return
        self.sync_btn.hide()
        self._say(tr("보관함과 비교하는 중..."))
        self._begin()

        def work():
            client, engine = self._open_engine()
            try:
                engine.load_manifest()
                pending = engine.plan()
                if not pending.is_empty:
                    return engine, None
                return engine, plan_rollback(engine.state_at(vid), engine.scan_result)
            finally:
                client.close()

        def planned(out):
            engine, plan = out
            self.engine = engine
            if plan is None:
                self._end()
                self._say(tr("동기화하지 않은 변경이 있습니다. 먼저 [지금 동기화]로 지금 상태를 버전으로 남긴 뒤 되돌리세요."), "WarnText")
                self.sync_btn.setVisible(self.request_sync is not None)
                return
            if plan.is_noop and engine.manifest.history and engine.manifest.history[-1].id == vid:
                self._end()
                return
            removed = "\n".join(f"  · {r}" for r in plan.remove[:8]) + (tr("\n  … 외 {v1}개", v1=len(plan.remove) - 8) if len(plan.remove) > 8 else "")
            text = (tr("받을 파일 {n}개 ({size})\n지울 파일 {n2}개", n=len(plan.fetch), size=human(plan.fetch_bytes), n2=len(plan.remove))
                    + (f"\n{removed}" if plan.remove else "")
                    + tr("\n\n지울 파일은 지금 버전에 남아 있어 다시 되돌릴 수 있습니다."))
            if not ask(self, tr("이 시점으로 되돌리기"), text, yes=tr("되돌리기")):
                self._end()
                self._say(tr("되돌리기를 취소했습니다."))
                return
            self._run_rollback(vid, plan)

        def failed(exc):
            self._end()
            self._say(tr("되돌리지 못했습니다: {v1}", v1=friendly(exc)), "ErrorText")

        run_async(work, planned, failed)

    def _run_rollback(self, vid: int, plan) -> None:
        s = self.session
        dk, root = s.settings.dk, s.root
        when = next((fmt_at(v.at) for v in self.engine.manifest.history if v.id == vid), tr("고른"))
        s.log(tr("{when} 버전으로 되돌리는 중", when=when), "busy")

        def work(progress):
            client, engine = self._open_engine()
            try:
                engine.load_manifest()
                result = apply_rollback(client, dk, root, plan, on_progress=progress)
                if not result.ok:
                    return engine, result
                engine.commit_state(plan.target, kind="rollback")
                return engine, result
            finally:
                client.close()

        def on_progress(p):
            if p.total_files:
                self._say(tr("받는 중 {done_files}/{total_files}개 · {size}/s\n{v1}", done_files=p.done_files, total_files=p.total_files, size=human(p.speed), v1=p.current.rpartition('/')[2]))

        def done(out):
            engine, result = out
            self.engine = engine
            self._end()
            if result.ok:
                s.log(tr("{when} 버전으로 되돌렸습니다", when=when) + (" · " if result.saved_as or plan.remove else "")
                      + counts_text([(tr("받은 파일"), len(result.saved_as)), (tr("지운 파일"), len(plan.remove))]))
                self.fill()
                self._say(tr("✓ 되돌렸습니다. 되돌리기도 새 버전으로 남았습니다."), "OkText")
            else:
                problems = len(result.failed) + len(result.corrupt)
                s.log(tr("파일 {problems}개를 받지 못해 되돌리기를 끝내지 못했습니다. 다시 되돌리면 이어서 받습니다.", problems=problems), "warn")
                self._say(tr("받지 못한 파일이 {problems}개 있어 목차는 바꾸지 않았습니다. 다시 되돌리면 이어서 받습니다.", problems=problems), "WarnText")

        def failed(exc):
            self._end()
            s.log(tr("되돌리지 못했습니다: {v1}", v1=friendly(exc)), "error")
            self._say(tr("되돌리지 못했습니다: {v1}", v1=friendly(exc)), "ErrorText")

        run_async(work, done, failed, on_progress)
