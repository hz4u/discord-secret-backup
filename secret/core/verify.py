import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..i18n import tr
from . import chunks, keys
from . import manifest as manifest_mod
from .crypto_core import DecryptionError
from .discord_api import DiscordError, NetworkError
from .manifest import FileEntry
from .restore import FILE_WORKERS, MEMORY_BUDGET, MemoryBudget, _Cancelled, _iter_parts, _UrlBook, find_indexes
from .sync_engine import SyncProgress, download_message_blob

GCM_TAG = 16


@dataclass
class Problem:
    rel: str
    reason: str


@dataclass
class VerifyResult:
    deep: bool = False
    total: int = 0
    ok: int = 0
    problems: list[Problem] = field(default_factory=list)
    latest_index_ok: bool = True
    password_opens_index: bool = True
    previous_index_ok: bool | None = None
    cancelled: bool = False
    aborted: str | None = None

    @property
    def healthy(self) -> bool:
        return (not self.problems and self.ok == self.total and self.latest_index_ok and self.password_opens_index
                and not self.cancelled and not self.aborted)


def _read_index(client, index_channel: str, msg: dict, dk: bytes):
    blob = download_message_blob(client, index_channel, msg)
    return blob, manifest_mod.decode_index(blob, dk)


def _refs(e: FileEntry) -> list[tuple[str, str]]:
    if e.thread_id is None:
        return [(e.channel_id, e.message_id)]
    return [(e.thread_id, pid) for pid in e.part_ids]


def _quick(book: _UrlBook, e: FileEntry) -> str | None:
    refs = _refs(e)
    atts = [book.listed(*ref) for ref in refs]
    missing = sum(a is None for a in atts)
    if missing:
        return tr("디스코드에 없습니다") if len(refs) == 1 else tr("조각 {missing}/{n}개가 디스코드에 없습니다", missing=missing, n=len(refs))
    if sum(int(a.get("size", -1)) for a in atts) != e.size + GCM_TAG * len(refs):
        return tr("디스코드에 있는 크기가 맞지 않습니다")
    return None


def _deep(book: _UrlBook, e: FileEntry, dk: bytes, cancel: threading.Event, budget: MemoryBudget) -> str | None:
    fid = bytes.fromhex(e.file_id)
    key = keys.file_key(dk, fid)
    prefix = bytes.fromhex(e.nonce_prefix)
    h = hashlib.sha256()
    try:
        for i, ct in enumerate(_iter_parts(book, e, cancel, budget)):
            h.update(chunks.decrypt_part(key, fid, prefix, i, e.n, ct))
    except DecryptionError:
        return tr("내용이 손상됐습니다 (암호 조각 검증 실패)")
    return None if h.hexdigest() == e.sha256 else tr("내용이 손상됐습니다 (지문이 맞지 않음)")


def verify_backup(
    client,
    guild_id: str,
    password: str,
    dk: bytes,
    deep: bool = False,
    on_progress=None,
    cancel: threading.Event | None = None,
    workers: int = FILE_WORKERS,
) -> VerifyResult:
    cancel = cancel or threading.Event()
    result = VerifyResult(deep=deep)
    index_channel, msgs = find_indexes(client, guild_id)

    m = None
    for i, msg in enumerate(msgs[:2]):
        try:
            blob, decoded = _read_index(client, index_channel, msg, dk)
        except (DecryptionError, manifest_mod.ManifestError, DiscordError, ValueError):
            if i == 0:
                result.latest_index_ok = False
            else:
                result.previous_index_ok = False
            continue
        if i == 1:
            result.previous_index_ok = True
        if m is None:
            m = decoded
            try:
                keys.unwrap(manifest_mod.read_index_header(blob), password)
            except DecryptionError:
                result.password_opens_index = False
    if m is None:
        raise manifest_mod.ManifestError("no readable index")

    result.total = len(m.files)
    progress = SyncProgress(phase="verify", total_files=result.total,
                            total_bytes=sum(e.size for e in m.files.values()) if deep else 0)
    lock = threading.Lock()
    book = _UrlBook(client)
    budget = MemoryBudget(MEMORY_BUDGET)
    started = time.monotonic()

    def emit(**kw):
        with lock:
            for k, v in kw.items():
                setattr(progress, k, v)
            if deep:
                elapsed = max(time.monotonic() - started, 1e-6)
                progress.speed = progress.done_bytes / elapsed
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
        try:
            reason = _quick(book, e)
            if reason is None and deep:
                reason = _deep(book, e, dk, cancel, budget)
        except _Cancelled:
            return
        except NetworkError as exc:
            cancel.set()
            with lock:
                result.aborted = str(exc)
            return
        except DiscordError as exc:
            reason = tr("받지 못했습니다 ({exc})", exc=exc)
        with lock:
            if reason:
                result.problems.append(Problem(rel, reason))
            else:
                result.ok += 1
            progress.done_files += 1
            progress.done_bytes += e.size if deep else 0
        emit()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(task, sorted(m.files.items())))
    result.problems.sort(key=lambda p: p.rel)
    result.cancelled = cancel.is_set() and not result.aborted and result.ok + len(result.problems) < result.total
    return result
