import hashlib
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import chunks, keys
from . import manifest as manifest_mod
from .config_store import ConfigStore, Settings
from .crypto_core import DecryptionError
from .discord_api import DiscordError, DiscordNotFound, NetworkError, locate_control
from .file_crypto import unique_path
from .hidden import is_hidden_or_system
from .manifest import FileEntry, Manifest
from .scanner import is_hidden_root_name
from .sync_engine import SyncProgress, download_message_blob

PART_SUFFIX = ".secret-part"
FILE_WORKERS = 6
PART_WINDOW = 3
MEMORY_BUDGET = 128 * 1024 * 1024


class NoBackupError(Exception):
    pass


@dataclass
class RestoreResult:
    total: int = 0
    verified: int = 0
    mismatched: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    skipped: int = 0
    cancelled: bool = False
    used_fallback_index: bool = False


def find_indexes(client, guild_id: str) -> tuple[str, list[dict]]:
    _, index, msgs = locate_control(client, guild_id, client.me()["id"])
    if index is None or not msgs:
        raise NoBackupError()
    return index, msgs


def open_backup(client, guild_id: str, password: str) -> tuple[Manifest, bytes, str, bool]:
    index, msgs = find_indexes(client, guild_id)
    errors: list[Exception] = []
    for i, msg in enumerate(msgs[:2]):
        try:
            blob = download_message_blob(client, index, msg)
            dk = keys.unwrap(manifest_mod.read_index_header(blob), password)
            return manifest_mod.decode_index(blob, dk), dk, msg["id"], i > 0
        except (DecryptionError, manifest_mod.ManifestError, DiscordError, ValueError) as exc:
            errors.append(exc)
    if all(isinstance(e, DecryptionError) for e in errors):
        raise errors[0]
    raise manifest_mod.ManifestError(f"no readable index: {errors}")


class _Cancelled(Exception):
    pass


def target_has_files(target: Path) -> bool:
    if not target.is_dir():
        return False
    return any(not (is_hidden_root_name(p.name) or is_hidden_or_system(p)) for p in target.iterdir())


class _UrlBook:
    def __init__(self, client):
        self.client = client
        self._urls: dict[tuple[str, str], str] = {}
        self._atts: dict[tuple[str, str], dict] = {}
        self._loaded: set[str] = set()
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _ensure(self, channel_id: str, missing_ok: bool = False) -> None:
        with self._guard:
            lock = self._locks.setdefault(channel_id, threading.Lock())
        with lock:
            if channel_id in self._loaded:
                return
            try:
                self._load(channel_id)
            except DiscordNotFound:
                if not missing_ok:
                    raise
                self._loaded.add(channel_id)

    def url(self, channel_id: str, message_id: str) -> str:
        self._ensure(channel_id)
        found = self._urls.get((channel_id, message_id))
        return found if found else self.fresh(channel_id, message_id)

    def listed(self, channel_id: str, message_id: str) -> dict | None:
        self._ensure(channel_id, missing_ok=True)
        return self._atts.get((channel_id, message_id))

    def fresh(self, channel_id: str, message_id: str) -> str:
        msg = self.client.get_message(channel_id, message_id)
        url = msg["attachments"][0]["url"]
        self._urls[(channel_id, message_id)] = url
        return url

    def _load(self, channel_id: str) -> None:
        after = "0"
        while True:
            batch = self.client.messages_after(channel_id, after, 100)
            for m in batch:
                if m.get("attachments"):
                    att = m["attachments"][0]
                    self._atts[(channel_id, m["id"])] = att
                    self._urls[(channel_id, m["id"])] = att["url"]
            if len(batch) < 100:
                break
            after = max(batch, key=lambda m: int(m["id"]))["id"]
        self._loaded.add(channel_id)

    def download(self, channel_id: str, message_id: str) -> bytes:
        try:
            return self.client.download(self.url(channel_id, message_id))
        except NetworkError:
            raise
        except DiscordError:
            return self.client.download(self.fresh(channel_id, message_id))


class MemoryBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._cv = threading.Condition()

    def try_acquire(self, n: int) -> bool:
        with self._cv:
            if self.used and self.used + n > self.limit:
                return False
            self.used += n
            return True

    def acquire(self, n: int, cancel: threading.Event) -> None:
        with self._cv:
            while self.used and self.used + n > self.limit:
                if cancel.is_set():
                    raise _Cancelled()
                self._cv.wait(0.1)
            self.used += n

    def release(self, n: int) -> None:
        with self._cv:
            self.used -= n
            self._cv.notify_all()


def _iter_parts(book: _UrlBook, entry: FileEntry, cancel: threading.Event, budget: MemoryBudget | None = None):
    budget = budget or MemoryBudget(1 << 62)
    if entry.thread_id is None:
        refs = [(entry.channel_id, entry.message_id)]
    else:
        refs = [(entry.thread_id, part_id) for part_id in entry.part_ids]
    n = len(refs)
    part_len = (entry.size if n == 1 else -(-entry.size // (n - 1))) + 16
    if len(refs) == 1:
        budget.acquire(part_len, cancel)
        try:
            if cancel.is_set():
                raise _Cancelled()
            yield book.download(*refs[0])
        finally:
            budget.release(part_len)
        return
    pool = ThreadPoolExecutor(max_workers=PART_WINDOW)
    held = 0
    try:
        todo = iter(refs)
        nxt = next(todo, None)
        ahead: deque = deque()
        while nxt or ahead:
            if cancel.is_set():
                raise _Cancelled()
            while nxt and len(ahead) < PART_WINDOW and budget.try_acquire(part_len):
                held += part_len
                ahead.append(pool.submit(book.download, *nxt))
                nxt = next(todo, None)
            if not ahead:
                budget.acquire(part_len, cancel)
                held += part_len
                ahead.append(pool.submit(book.download, *nxt))
                nxt = next(todo, None)
            data = ahead.popleft().result()
            yield data
            del data
            budget.release(part_len)
            held -= part_len
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        if held:
            budget.release(held)


def download_entry(book: "_UrlBook", budget: MemoryBudget, dk: bytes, e: FileEntry, dest: Path,
                   cancel: threading.Event) -> bool:
    fid = bytes.fromhex(e.file_id)
    key = keys.file_key(dk, fid)
    prefix = bytes.fromhex(e.nonce_prefix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = dest.with_name(dest.name + PART_SUFFIX)
    try:
        h = hashlib.sha256()
        with open(tmp, "wb") as out:
            for i, ct in enumerate(_iter_parts(book, e, cancel, budget)):
                data = chunks.decrypt_part(key, fid, prefix, i, e.n, ct)
                h.update(data)
                out.write(data)
        if h.hexdigest() != e.sha256:
            os.replace(tmp, dest.with_name(dest.name + ".corrupt"))
            tmp = None
            return False
        os.replace(tmp, dest)
        tmp = None
        os.utime(dest, ns=(e.mtime_ns, e.mtime_ns))
        return True
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def read_entry(book: "_UrlBook", budget: MemoryBudget, dk: bytes, e: FileEntry, cancel: threading.Event) -> bytes:
    fid = bytes.fromhex(e.file_id)
    key = keys.file_key(dk, fid)
    prefix = bytes.fromhex(e.nonce_prefix)
    data = b"".join(chunks.decrypt_part(key, fid, prefix, i, e.n, ct) for i, ct in enumerate(_iter_parts(book, e, cancel, budget)))
    if hashlib.sha256(data).hexdigest() != e.sha256:
        raise DecryptionError("fingerprint mismatch")
    return data


def _sha256(path: Path, cancel: threading.Event) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            if cancel.is_set():
                raise _Cancelled()
            h.update(block)
    return h.hexdigest()


def _already_there(dest: Path, e: FileEntry, cancel: threading.Event) -> Path | None:
    stem, suffix = dest.stem, dest.suffix
    n = 0
    candidate = dest
    while candidate.exists():
        if candidate.is_file() and candidate.stat().st_size == e.size and _sha256(candidate, cancel) == e.sha256:
            return candidate
        n += 1
        candidate = dest.with_name(f"{stem} ({n}){suffix}")
    return None


def _remove_stale_parts(target: Path, m: Manifest) -> None:
    for folder in {(target / rel).parent for rel in m.files}:
        if folder.is_dir():
            for p in folder.glob("*" + PART_SUFFIX):
                try:
                    p.unlink()
                except OSError:
                    pass


def restore(
    client,
    guild_id: str,
    password: str,
    target: Path,
    on_progress=None,
    cancel: threading.Event | None = None,
    workers: int = FILE_WORKERS,
    write_config: bool = False,
    token: str = "",
) -> RestoreResult:
    cancel = cancel or threading.Event()
    m, dk, index_id, fallback = open_backup(client, guild_id, password)
    result = RestoreResult(total=len(m.files), used_fallback_index=fallback)
    progress = SyncProgress(phase="restore", total_files=len(m.files), total_bytes=sum(e.size for e in m.files.values()))
    lock = threading.Lock()
    book = _UrlBook(client)
    budget = MemoryBudget(MEMORY_BUDGET)
    started = time.monotonic()

    def emit(**kw):
        with lock:
            for k, v in kw.items():
                setattr(progress, k, v)
            elapsed = max(time.monotonic() - started, 1e-6)
            progress.speed = progress.done_bytes / elapsed
            left = progress.total_bytes - progress.done_bytes
            progress.eta = left / progress.speed if progress.speed else None
            snap = SyncProgress(**vars(progress))
        if on_progress:
            on_progress(snap)

    for d in m.dirs:
        (target / d).mkdir(parents=True, exist_ok=True)
    _remove_stale_parts(target, m)

    def task(item: tuple[str, FileEntry]) -> None:
        rel, e = item
        if cancel.is_set():
            return
        emit(current=rel, parts=e.n)
        try:
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if _already_there(dest, e, cancel):
                with lock:
                    result.verified += 1
                    result.skipped += 1
            else:
                if dest.exists():
                    final = unique_path(dest.parent, dest.name)
                    with lock:
                        result.renamed.append((rel, final.relative_to(target).as_posix()))
                    dest = final
                if download_entry(book, budget, dk, e, dest, cancel):
                    with lock:
                        result.verified += 1
                else:
                    with lock:
                        result.mismatched.append(rel)
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
        with lock:
            progress.done_files += 1
            progress.done_bytes += e.size
        emit()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(task, sorted(m.files.items())))
    result.cancelled = cancel.is_set() and len(result.failed) + result.verified + len(result.mismatched) < result.total

    if write_config and not cancel.is_set():
        cfg = target / ".secret" / "config.dat"
        if not ConfigStore.exists(cfg):
            ConfigStore.create(
                cfg, password,
                Settings(dk=dk, token=token, guild_id=guild_id, index_message_id=index_id, manifest=m.to_json()),
            )
    return result
