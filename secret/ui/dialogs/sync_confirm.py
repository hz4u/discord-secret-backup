import time

from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QListWidget

from ...core.planner import Plan
from ...i18n import tr
from .. import theme
from ..right_panel import fmt_duration
from ..sidebar import human
from ..widgets import Card, Dialog, button, icon_label, label

DEFAULT_SPEED = 2 * 1024 * 1024
OPEN_GUARD = 0.4


def estimate_speed(history: list[dict]) -> float | None:
    for h in history:
        if h.get("bytes", 0) > 1024 * 1024 and h.get("seconds"):
            return h["bytes"] / h["seconds"]
    return None


class SyncConfirmDialog(Dialog):
    def __init__(self, parent, plan: Plan, sizes: dict[str, int], history: list[dict], drive: str = "USB"):
        super().__init__(parent, 560)
        self.heading(tr("동기화 확인"), tr("아래 내용대로 디스코드 백업을 맞춥니다."), "arrows-clockwise")

        summary = Card()
        grid = QGridLayout()
        grid.setVerticalSpacing(10)
        changed_bytes = sum(sizes.get(r, 0) for r in plan.changed)
        rows = [
            ("plus-circle", theme.OK, tr("새로 올릴 파일"), len(plan.new), human(plan.upload_bytes - changed_bytes)),
            ("pencil-simple", theme.ACCENT, tr("바뀌어서 다시 올릴 파일"), len(plan.changed), human(changed_bytes)),
            ("trash", theme.DANGER, tr("디스코드에서 지울 파일"), len(plan.deleted), ""),
            ("prohibit", theme.FAINT, tr("백업 제외"), len(plan.excluded), ""),
        ]
        if plan.remap_channels:
            rows.append(("folder-simple", theme.MUTED, tr("이름이 바뀐 폴더 (목차만 고침)"), plan.renamed_folders, ""))
        if plan.renamed or plan.touched:
            rows.append(("arrows-left-right", theme.MUTED, tr("목차만 고칠 파일 (이름·위치·시각 변경)"), len(plan.renamed) + len(plan.touched), ""))
        for i, (icon, color, text, count, size) in enumerate(rows):
            grid.addWidget(icon_label(icon, color, 18), i, 0)
            grid.addWidget(label(text), i, 1)
            n = label(tr("{count}개", count=count))
            n.setStyleSheet("font-weight: 600;")
            grid.addWidget(n, i, 2)
            grid.addWidget(label(size, "Muted"), i, 3)
        grid.setColumnStretch(1, 1)
        summary.body.addLayout(grid)
        new_channels = len(plan.create_channels) + len(plan.create_categories)
        if new_channels:
            summary.body.addWidget(label(tr("새로 만들 카테고리·채널 {new_channels}개", new_channels=new_channels), "Muted"))
        speed = estimate_speed(history)
        eta = plan.upload_bytes / (speed or DEFAULT_SPEED)
        guess = "" if speed else tr(" (지난 기록이 없어 2MB/s로 가정)")
        if plan.upload_bytes:
            summary.body.addWidget(label(tr("예상 시간 약 {v1}{guess}", v1=fmt_duration(eta), guess=guess), "Muted"))
        self.body.addWidget(summary)

        if plan.near_limit:
            self.body.addWidget(label(
                tr("⚠ 디스코드 채널이 {channel_count_after}개가 됩니다 (한도 500). 폴더 수를 줄이는 것을 권장합니다.", channel_count_after=plan.channel_count_after),
                "WarnText", wrap=True))

        self.delete_check = None
        if plan.deleted:
            self._expandable(tr("지울 파일 {n}개 보기", n=len(plan.deleted)), plan.deleted)
            self.delete_check = QCheckBox(tr("{drive}에서 지운 파일 {n}개를 디스코드 백업에서도 지우는 것을 확인했습니다", drive=drive, n=len(plan.deleted)))
            self.delete_check.toggled.connect(self._update)
            self.body.addWidget(self.delete_check)
        if plan.excluded:
            self._expandable(tr("백업 제외 파일 {n}개 보기", n=len(plan.excluded)), plan.excluded)

        row = QHBoxLayout()
        row.addStretch()
        cancel = button(tr("취소"))
        cancel.clicked.connect(self.reject)
        self.start = button(tr("동기화 시작"), "Primary")
        self.start.clicked.connect(self._start)
        for b in (cancel, self.start):
            b.setAutoDefault(False)
            b.setDefault(False)
        self._opened = time.monotonic()
        row.addWidget(cancel)
        row.addWidget(self.start)
        self.body.addLayout(row)
        self._update()

    def _expandable(self, title: str, rows: list[str]) -> None:
        toggle = button(f"▸ {title}", "Link")
        view = QListWidget()
        view.addItems(rows)
        view.setMaximumHeight(140)
        view.hide()

        def flip():
            view.setVisible(not view.isVisible())
            toggle.setText(f"{'▾' if view.isVisible() else '▸'} {title}")
            self.adjustSize()

        toggle.clicked.connect(flip)
        self.body.addWidget(toggle)
        self.body.addWidget(view)

    def showEvent(self, event):  # noqa: N802
        self._opened = time.monotonic()
        super().showEvent(event)

    def _start(self) -> None:
        if time.monotonic() - self._opened < OPEN_GUARD:
            return
        self.accept()

    def _update(self) -> None:
        self.start.setEnabled(self.delete_check is None or self.delete_check.isChecked())
