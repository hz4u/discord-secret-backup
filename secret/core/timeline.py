import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .crypto_core import DecryptionError
from .discord_api import DiscordError, NetworkError
from .file_crypto import unique_path
from .manifest import FileEntry, Manifest
from .restore import FILE_WORKERS, MEMORY_BUDGET, MemoryBudget, _Cancelled, _UrlBook, download_entry
from .scanner import ScanResult, channel_key, logical_folder
from .sync_engine import SyncProgress


@dataclass
class RollbackPlan:
    target: Manifest
    fetch: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    fetch_bytes: int = 0
    remove_dirs: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not (self.fetch or self.remove)


@dataclass
class FetchResult:
    saved_as: dict[str, str] = field(default_factory=dict)
    corrupt: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not (self.corrupt or self.failed or self.cancelled)


def plan_rollback(target: Manifest, scan: ScanResult) -> RollbackPlan:
    fetch = [rel for rel, e in sorted(target.files.items())
             if (f := scan.files.get(rel)) is None or (f.size, f.mtime_ns) != (e.size, e.mtime_ns)]
    remove = sorted(rel for rel in scan.files if rel not in target.files)
    remove_dirs = sorted(set(scan.dirs) - set(target.dirs), key=lambda d: (-d.count("/"), d))
    return RollbackPlan(target, fetch, remove, sum(target.files[r].size for r in fetch), remove_dirs)


def fetch(
    client,
    dk: bytes,
    root: Path,
    items: dict[str, FileEntry],
    *,
    overwrite: bool,
    cancel: threading.Event | None = None,
    on_progress=None,
    workers: int = FILE_WORKERS,
) -> FetchResult:
    cancel = cancel or threading.Event()
    result = FetchResult()
    book = _UrlBook(client)
    budget = MemoryBudget(MEMORY_BUDGET)
    lock = threading.Lock()
    progress = SyncProgress(phase="fetch", total_files=len(items), total_bytes=sum(e.size for e in items.values()))
    started = time.monotonic()

    def emit(**kw):
        with lock:
            for k, v in kw.items():
                setattr(progress, k, v)
            progress.speed = progress.done_bytes / max(time.monotonic() - started, 1e-6)
            left = progress.total_bytes - progress.done_bytes
            progress.eta = left / progress.speed if progress.speed else None
            snap = SyncProgress(**vars(progress))
        if on_progress:
            on_progress(snap)

    def task(item: tuple[str, FileEntry]) -> None:
        rel, e = item
        if cancel.is_set():
            return
        emit(current=rel)
        dest = root / rel
        if not overwrite and dest.exists():
            dest = unique_path(dest.parent, dest.name)
        try:
            ok = download_entry(book, budget, dk, e, dest, cancel)
        except _Cancelled:
            return
        except NetworkError:
            cancel.set()
            with lock:
                result.failed.append((rel, "network"))
            return
        except (DiscordError, DecryptionError, OSError) as exc:
            with lock:
                result.failed.append((rel, str(exc)))
            return
        with lock:
            if ok:
                result.saved_as[rel] = dest.relative_to(root).as_posix()
            else:
                result.corrupt.append(rel)
            progress.done_files += 1
            progress.done_bytes += e.size
        emit()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(task, sorted(items.items())))
    result.cancelled = cancel.is_set() and not result.failed and len(result.saved_as) + len(result.corrupt) < len(items)
    return result


def apply_rollback(client, dk: bytes, root: Path, plan: RollbackPlan, cancel=None, on_progress=None,
                   workers: int = FILE_WORKERS) -> FetchResult:
    for d in plan.target.dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    result = fetch(client, dk, root, {rel: plan.target.files[rel] for rel in plan.fetch}, overwrite=True,
                   cancel=cancel, on_progress=on_progress, workers=workers)
    if not result.ok:
        return result
    for rel in plan.remove:
        try:
            (root / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            result.failed.append((rel, str(exc)))
    for d in plan.remove_dirs:
        try:
            (root / d).rmdir()
        except OSError:
            pass
    return result


def restored_state(m: Manifest, restored: dict[str, FileEntry]) -> Manifest:
    state = Manifest(categories=dict(m.categories), channels=dict(m.channels), files=dict(m.files), dirs=list(m.dirs))
    for rel, e in restored.items():
        state.files[rel] = e
        top, sub = logical_folder(rel)
        key = channel_key(top, sub)
        if key not in state.channels and e.channel_id not in state.channels.values():
            state.channels[key] = e.channel_id
            cat = m.retired_channels.get(e.channel_id)
            if top not in state.categories and cat:
                state.categories[top] = cat
        parts = rel.split("/")[:-1]
        for i in range(len(parts)):
            d = "/".join(parts[: i + 1])
            if d not in state.dirs:
                state.dirs.append(d)
    state.dirs.sort()
    return state
