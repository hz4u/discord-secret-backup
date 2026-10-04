import shutil
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..core import prefs
from ..core.config_store import ConfigStore, Settings
from ..core.discord_api import HttpDiscord
from ..core.drive import DriveKind
from ..core.drive import detect as detect_drive
from ..core.hidden import hide
from ..core.keys import new_data_key
from ..core.manifest import Manifest
from ..core.planner import Pending, quick_pending
from ..core.scanner import ScanResult, scan
from ..i18n import tr
from .worker import run_async


class Session(QObject):
    scan_changed = Signal()
    lock_changed = Signal()
    settings_changed = Signal()
    usb_changed = Signal(bool)
    prefs_changed = Signal(str)
    log_event = Signal(str, str)

    def __init__(self, root: Path, client_factory=HttpDiscord):
        super().__init__()
        self.root = root
        self.prefs = prefs.load(root)
        self._drive: DriveKind | None = None
        self.client_factory = client_factory
        self.store: ConfigStore | None = None
        self.scan: ScanResult = ScanResult()
        self.pending: Pending | None = None
        self.usb_present = True
        self.sync_running = False
        self._scan_generation = 0
        self._last_counts = None
        self._last_unreadable: list[str] = []
        if self.secret_dir.exists():
            hide(self.secret_dir)
        shutil.rmtree(self.secret_dir / "thumbs", ignore_errors=True)

    @property
    def drive(self) -> DriveKind:
        if self._drive is None:
            self._drive = detect_drive(self.root)
        return self._drive

    def pref(self, key: str, default):
        return self.prefs.get(key, default)

    def set_pref(self, key: str, value) -> None:
        self.prefs[key] = value
        try:
            prefs.save(self.root, self.prefs)
        except OSError:
            pass
        self.prefs_changed.emit(key)

    @property
    def secret_dir(self) -> Path:
        return self.root / ".secret"

    @property
    def config_path(self) -> Path:
        return self.secret_dir / "config.dat"


    @property
    def has_config(self) -> bool:
        return ConfigStore.exists(self.config_path)

    @property
    def unlocked(self) -> bool:
        return self.store is not None

    @property
    def settings(self) -> Settings | None:
        return self.store.settings if self.store else None

    @property
    def connected(self) -> bool:
        return bool(self.store and self.store.settings.connected)

    def unlock_async(self, password: str, on_ok, on_err, source: Path | None = None) -> None:
        def done(store):
            self.store = store
            self._after_lock_change()
            on_ok()

        run_async(lambda: ConfigStore.open(self.config_path, password, source=source), done, on_err)

    def create_async(self, password: str, on_ok, on_err, **settings) -> None:
        def done(store):
            self.store = store
            self._after_lock_change()
            on_ok()

        run_async(lambda: ConfigStore.create(self.config_path, password, Settings(dk=new_data_key(), **settings)), done, on_err)

    def lock(self) -> None:
        if self.store is None:
            return
        self.store = None
        self._after_lock_change()

    def log(self, text: str, level: str = "info") -> None:
        self.log_event.emit(text, level)

    @property
    def hidden_folders(self) -> set[str]:
        return set(self.store.settings.hidden_folders) if self.store else set()

    def _after_lock_change(self) -> None:
        self.log(tr("잠금을 풀었습니다") if self.store else tr("잠갔습니다"))
        self.refresh_scan()
        self.recompute_pending()
        self.lock_changed.emit()
        self.settings_changed.emit()

    def settings_saved(self) -> None:
        self.recompute_pending()
        self.settings_changed.emit()

    def refresh_scan(self) -> None:
        self._scan_generation += 1
        generation = self._scan_generation
        root = self.root

        def done(result: ScanResult | None) -> None:
            if result is None or generation != self._scan_generation:
                return
            self.scan = result
            self.recompute_pending()
            self.scan_changed.emit()
            counts = (len(result.files) + len(result.excluded), len(result.dirs))
            if counts != self._last_counts:
                self._last_counts = counts
                self.log(tr("보관함을 읽었습니다 · 파일 {v1}개 · 폴더 {v2}개", v1=counts[0], v2=counts[1]))
            unreadable = sorted(result.unreadable)
            if unreadable and unreadable != self._last_unreadable:
                names = ", ".join(unreadable[:3]) + (tr(" 외 {v1}개", v1=len(unreadable) - 3) if len(unreadable) > 3 else "")
                self.log(tr("읽을 수 없는 폴더·파일이 있습니다: {names} · 드라이브가 손상됐을 수 있으니 드라이브 검사를 해 주세요. 그 안의 백업은 지우지 않고 그대로 둡니다.", names=names), "warn")
            self._last_unreadable = unreadable

        include = self.hidden_folders
        run_async(lambda: scan(root, include_hidden=include) if root.exists() else None, done, lambda exc: None)

    def manifest(self) -> Manifest | None:
        if self.store is None:
            return None
        cached = self.store.settings.manifest
        return Manifest.from_json(cached) if cached else Manifest.empty()

    def recompute_pending(self) -> None:
        m = self.manifest()
        self.pending = quick_pending(m, self.scan, force=set(self.store.settings.reupload)) if m is not None else None

    def file_state(self, rel: str, size: int, mtime_ns: int, excluded: bool) -> str:
        if excluded:
            return "excluded"
        if self.store is None:
            return ""
        files = (self.store.settings.manifest or {}).get("files", {})
        e = files.get(rel)
        if e and e["size"] == size and e["mtime_ns"] == mtime_ns and rel not in self.store.settings.reupload:
            return "synced"
        return "pending"
