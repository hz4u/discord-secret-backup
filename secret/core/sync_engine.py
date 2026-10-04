import hashlib
import re
import secrets
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..i18n import tr
from . import chunks, history, keys, scanner
from . import manifest as manifest_mod
from .config_store import ConfigStore
from .crypto_core import DecryptionError
from .discord_api import (
    CONTROL_CATEGORY,
    INDEX_CHANNEL,
    INDEX_PREFIX,
    DiscordError,
    DiscordNotFound,
    NetworkError,
    locate_control,
)
from .manifest import FileEntry, Manifest
from .planner import Plan, make_plan, sha256_file

INDEX_KEEP = 2
HISTORY_KEEP = 20


def _rand_name(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


@dataclass
class SyncProgress:
    phase: str = "idle"
    done_files: int = 0
    total_files: int = 0
    done_bytes: int = 0
    total_bytes: int = 0
    current: str = ""
    part: int = 0
    parts: int = 0
    speed: float = 0.0
    eta: float | None = None
    waiting: float = 0.0


@dataclass
class SyncResult:
    uploaded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    kept_channels: list[str] = field(default_factory=list)
    duration: float = 0.0
    cancelled: bool = False
    aborted: str | None = None

    @property
    def complete(self) -> bool:
        return not (self.cancelled or self.aborted or self.failed)


SAVE_INTERVAL = 2.0
LONG_WAIT_LOG = 10.0


def interleave_by_channel(uploads: list[str]) -> list[str]:
    groups: dict[str, deque] = {}
    for rel in uploads:
        groups.setdefault(scanner.channel_key(*scanner.logical_folder(rel)), deque()).append(rel)
    out = []
    while groups:
        for key in list(groups):
            out.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]
    return out


class SyncCancelled(Exception):
    pass


class _Stop(Exception):
    pass


class SyncEngine:
    def __init__(
        self,
        root: Path,
        client,
        store: ConfigStore,
        on_progress=None,
        cancel: threading.Event | None = None,
        workers: int = 4,
        part_size: int | None = None,
        clock=time.monotonic,
        on_event=None,
        keep_versions: int | None = None,
        keep_trash: int | None = None,
    ):
        self.root = root
        s = store.settings
        self.keep_versions = keep_versions if keep_versions is not None else (s.keep_versions or history.KEEP_VERSIONS)
        self.keep_trash = keep_trash if keep_trash is not None else (s.keep_trash or history.KEEP_TRASH)
        self.client = client
        self.store = store
        self.on_progress = on_progress
        self.on_event = on_event
        self.cancel = cancel or threading.Event()
        self.workers = workers
        self.clock = clock
        self._part_size_override = part_size
        self.progress = SyncProgress()
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._stop = threading.Event()
        self._speed_window: deque = deque()
        self._kept_parents: set[str] = set()
        self.manifest: Manifest | None = None
        self.scan_result: scanner.ScanResult | None = None
        self.index_messages: list[dict] = []
        self._broken_index: list[dict] = []
        if hasattr(client, "on_wait"):
            client.on_wait = self._on_wait

    @property
    def settings(self):
        return self.store.settings

    @property
    def guild_id(self) -> str:
        return self.settings.guild_id

    def _emit(self, **changes) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(self.progress, k, v)
            snapshot = SyncProgress(**asdict(self.progress))
        if self.on_progress:
            self.on_progress(snapshot)

    def _event(self, text: str, level: str = "info") -> None:
        if self.on_event:
            self.on_event(text, level)

    def _on_wait(self, seconds: float) -> None:
        self._emit(waiting=seconds)
        if seconds >= LONG_WAIT_LOG:
            self._event(tr("디스코드 요청 한도 때문에 {seconds:.0f}초 기다립니다", seconds=seconds), "warn")

    def _add_bytes(self, n: int) -> None:
        now = self.clock()
        with self._lock:
            self._speed_window.append((now, n))
            while self._speed_window and now - self._speed_window[0][0] > 10:
                self._speed_window.popleft()
            span = max(now - self._speed_window[0][0], 1e-6) if len(self._speed_window) > 1 else 0
            total = sum(b for _, b in self._speed_window)
            speed = total / span if span else 0.0
            done = self.progress.done_bytes + n
            remaining = max(self.progress.total_bytes - done, 0)
        self._emit(done_bytes=done, speed=speed, eta=(remaining / speed) if speed else None, waiting=0.0)

    def load_manifest(self) -> Manifest:
        self._emit(phase="prepare")
        me = self.client.me()
        self.bot_id = me["id"]
        guild = self.client.guild(self.guild_id)
        self.guild = guild
        limit = chunks.upload_limit_for_tier(int(guild.get("premium_tier", 0)))
        self.part_size = self._part_size_override or chunks.part_size_for_limit(limit)

        self.control_category, self.index_channel, self.index_messages = locate_control(
            self.client, self.guild_id, self.bot_id, self.settings.index_channel_id)
        if self.index_channel is None:
            self.manifest = Manifest.empty()
            return self.manifest
        if not self.index_messages:
            self.manifest = Manifest.empty()
        elif self.index_messages[0]["id"] == self.settings.index_message_id and self.settings.manifest:
            self.manifest = Manifest.from_json(self.settings.manifest)
        else:
            self.manifest = self._download_index(self.index_messages)
        return self.manifest

    def _download_index(self, messages: list[dict]) -> Manifest:
        last_error = None
        for i, msg in enumerate(messages):
            try:
                blob = download_message_blob(self.client, self.index_channel, msg)
                m = manifest_mod.decode_index(blob, self.settings.dk)
            except (DiscordError, manifest_mod.ManifestError, DecryptionError, ValueError) as exc:
                last_error = exc
                continue
            self._broken_index = messages[:i]
            self.index_messages = messages[i:]
            return m
        raise manifest_mod.ManifestError(f"no readable index: {last_error}")

    def plan(self) -> Plan:
        if self.manifest is None:
            self.load_manifest()
        self.scan_result = scanner.scan(self.root, include_hidden=self.settings.hidden_folders)
        force = set(self.settings.reupload) & set(self.scan_result.files)
        return make_plan(self.manifest, self.scan_result, self.root, force=force)

    def _journal(self) -> dict:
        j = self.settings.journal
        for key, default in (("created", []), ("categories", {}), ("channels", {}), ("files", {}), ("done", {}),
                             ("to_delete", [])):
            j.setdefault(key, type(default)())
        return j

    def _record(self, kind: str, obj_id: str, channel_id: str | None = None) -> None:
        with self._lock:
            self._journal()["created"].append({"kind": kind, "id": obj_id, "channel": channel_id})

    def _save(self, throttle: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if throttle and now - self._last_save < SAVE_INTERVAL:
                return
            self.store.save()
            self._last_save = now

    def _check_stop(self) -> None:
        if self.cancel.is_set():
            self._stop.set()
            raise SyncCancelled()
        if self._stop.is_set():
            raise _Stop()

    def run(self, plan: Plan) -> SyncResult:
        started = self.clock()
        result = SyncResult()
        m = Manifest.from_json(self.manifest.to_json())
        existing_ids = {c["id"] for c in self.client.channels(self.guild_id)}
        uploads = plan.uploads
        total_bytes = sum(self.scan_result.files[r].size for r in uploads)
        self._emit(phase="prepare", total_files=len(uploads), total_bytes=total_bytes, done_files=0, done_bytes=0)

        try:
            self._ensure_control(existing_ids)
            self._apply_folder_renames(plan, m)
            self._ensure_channels(plan, m, existing_ids)
            self._emit(phase="upload")
            self._upload_all(uploads, m, result)
        except SyncCancelled:
            result.cancelled = True
        except NetworkError as exc:
            result.aborted = str(exc)
        if result.cancelled or result.aborted:
            self._save()
            result.duration = self.clock() - started
            return result

        for old, new in plan.renamed:
            m.files[new] = m.files.pop(old)
            if old.rpartition("/")[2] != new.rpartition("/")[2]:
                self._event(tr("이름 바뀜: {old} → {new}", old=old, new=new))
        for rel in plan.touched:
            m.files[rel].mtime_ns = self.scan_result.files[rel].mtime_ns

        try:
            deleted = self._apply_deletions(plan, m) if not result.failed else []
            m.dirs = list(plan.dirs)
            out = self._record_version(m, kind="sync", deleted=deleted)
            result.deleted = list(deleted)
            in_trash = {t.rel for t in out.manifest.trash}
            if len(deleted) > 20:
                kept = sum(1 for rel in deleted if rel in in_trash)
                self._event(tr("삭제된 파일 {n}개를 반영했습니다 (휴지통으로 {kept}개)", n=len(deleted), kept=kept))
            else:
                for rel in deleted:
                    self._event(tr("삭제됨: {rel}", rel=rel) + (tr(" (휴지통으로)") if rel in in_trash else ""))
            self._emit(phase="index")
            m.touch()
            self._publish_index(m)
            self._emit(phase="delete")
            self._run_deletes(m, result)
            self._cleanup(m)
        except NetworkError as exc:
            result.aborted = str(exc)
            self._save()
            result.duration = self.clock() - started
            return result

        result.duration = self.clock() - started
        s = self.settings
        done = set(result.uploaded)
        s.reupload = [r for r in s.reupload if r not in done and r in self.scan_result.files]
        s.last_sync = datetime.now().isoformat(timespec="seconds")
        s.history = ([{
            "at": s.last_sync, "uploaded": len(result.uploaded), "deleted": len(result.deleted),
            "failed": len(result.failed), "seconds": round(result.duration, 1),
            "bytes": self.progress.done_bytes,
        }] + s.history)[:HISTORY_KEEP]
        self._save()
        self.manifest = m
        self._emit(phase="done")
        return result

    def _ensure_control(self, existing: set[str]) -> None:
        if self.control_category not in existing:
            self.control_category = self.client.create_category(self.guild_id, CONTROL_CATEGORY)
            self._record("channel", self.control_category)
            self.index_channel = None
        if self.index_channel not in existing:
            self.index_channel = self.client.create_text_channel(self.guild_id, INDEX_CHANNEL, self.control_category)
            self._record("channel", self.index_channel)

    def _apply_folder_renames(self, plan: Plan, m: Manifest) -> None:
        for new, old in plan.remap_categories.items():
            if old in m.categories:
                m.categories[new] = m.categories.pop(old)
        for new, old in plan.remap_channels.items():
            if old in m.channels:
                m.channels[new] = m.channels.pop(old)
        if plan.remap_channels:
            moved = sum(1 for old, new in plan.renamed if old.rpartition("/")[2] == new.rpartition("/")[2])
            self._event(tr("이름이 바뀐 폴더 {renamed_folders}개를 반영했습니다 (파일 {moved}개는 다시 올리지 않음)", renamed_folders=plan.renamed_folders, moved=moved))

    def _ensure_channels(self, plan: Plan, m: Manifest, existing: set[str]) -> None:
        j = self._journal()
        made = [0, 0]
        for top in plan.create_categories:
            cid = j["categories"].get(top)
            if cid not in existing:
                cid = self.client.create_category(self.guild_id, _rand_name("v"))
                made[0] += 1
                self._record("channel", cid)
                j["categories"][top] = cid
                existing.add(cid)
            m.categories[top] = cid
        for key in plan.create_channels:
            cid = j["channels"].get(key)
            if cid not in existing:
                top = key.split("/", 1)[0]
                cid = self.client.create_text_channel(self.guild_id, _rand_name("c"), m.categories[top])
                made[1] += 1
                self._record("channel", cid)
                j["channels"][key] = cid
                existing.add(cid)
            m.channels[key] = cid
        if any(made):
            self._event(tr("디스코드에 카테고리 {v1}개 · 채널 {v2}개를 만들었습니다", v1=made[0], v2=made[1]))
        self._save()

    def _upload_all(self, uploads: list[str], m: Manifest, result: SyncResult) -> None:
        self._stop.clear()
        errors: list[BaseException] = []

        def task(rel: str) -> None:
            try:
                self._check_stop()
                entry = self._upload_one(rel, m)
            except (SyncCancelled, _Stop):
                return
            except NetworkError as exc:
                self._stop.set()
                errors.append(exc)
                self._event(tr("인터넷 연결 문제로 멈췄습니다: {exc}", exc=exc), "error")
                return
            except (DiscordError, OSError) as exc:
                with self._lock:
                    result.failed.append((rel, str(exc)))
                self._event(tr("올리지 못함: {rel} ({exc})", rel=rel, exc=exc), "error")
                return
            with self._lock:
                if entry is None:
                    result.deferred.append(rel)
                else:
                    result.uploaded.append(rel)
                done = self.progress.done_files + 1
            if entry is None:
                self._event(tr("올리는 동안 바뀌어 다음에 올림: {rel}", rel=rel), "warn")
            else:
                self._event(tr("올림: {rel}", rel=rel))
            self._emit(done_files=done)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(task, interleave_by_channel(uploads)))
        if self.cancel.is_set():
            raise SyncCancelled()
        if errors:
            raise errors[0]

    def _upload_one(self, rel: str, m: Manifest) -> FileEntry | None:
        f = self.scan_result.files[rel]
        channel_id = m.channels[scanner.channel_key(*scanner.logical_folder(rel))]
        j = self._journal()
        signature = {"size": f.size, "mtime_ns": f.mtime_ns, "channel_id": channel_id}

        with self._lock:
            done = j["done"].get(rel)
        if done and all(done[k] == v for k, v in signature.items()):
            entry = FileEntry(**done["entry"])
            self._finish(rel, entry, m, j)
            self._add_bytes(f.size)
            return entry

        with self._lock:
            state = j["files"].get(rel)
            if not state or any(state[k] != v for k, v in signature.items()) or state.get("part_size") != self.part_size:
                state = {
                    **signature,
                    "part_size": self.part_size,
                    "file_id": chunks.new_file_id().hex(),
                    "nonce_prefix": chunks.new_nonce_prefix().hex(),
                    "n": chunks.part_count(f.size, self.part_size),
                    "message_id": None,
                    "thread_id": None,
                    "parts": {},
                }
                j["files"][rel] = state
        file_id, prefix, n = bytes.fromhex(state["file_id"]), bytes.fromhex(state["nonce_prefix"]), state["n"]
        key = keys.file_key(self.settings.dk, file_id)
        path = self.root / rel
        hasher = hashlib.sha256() if not state["parts"] else None
        self._emit(current=rel, part=0, parts=n)

        if n == 1:
            if "0" not in state["parts"]:
                data = chunks.read_part(path, 0, self.part_size)
                if hasher:
                    hasher.update(data)
                self._check_stop()
                msg = self.client.send_file(
                    channel_id, state["file_id"], f"{secrets.token_hex(8)}.bin",
                    chunks.encrypt_part(key, file_id, prefix, 0, 1, data),
                )
                self._record("message", msg["id"], channel_id)
                with self._lock:
                    state["message_id"] = state["parts"]["0"] = msg["id"]
                self._add_bytes(len(data))
                self._save(throttle=True)
        else:
            if state["message_id"] is None:
                msg = self.client.send_message(channel_id, state["file_id"])
                self._record("message", msg["id"], channel_id)
                with self._lock:
                    state["message_id"] = msg["id"]
            if state["thread_id"] is None:
                tid = self.client.create_thread(channel_id, state["message_id"], _rand_name("t"))
                self._record("thread", tid)
                with self._lock:
                    state["thread_id"] = tid
                self._save(throttle=True)
            stem = secrets.token_hex(8)
            for i in range(n):
                if str(i) in state["parts"]:
                    continue
                self._check_stop()
                data = chunks.read_part(path, i, self.part_size)
                if hasher:
                    hasher.update(data)
                self._emit(current=rel, part=i + 1, parts=n)
                msg = self.client.send_file(
                    state["thread_id"], "", f"{stem}.{i + 1:03d}",
                    chunks.encrypt_part(key, file_id, prefix, i, n, data),
                )
                self._record("message", msg["id"], state["thread_id"])
                with self._lock:
                    state["parts"][str(i)] = msg["id"]
                self._add_bytes(len(data))
                self._save(throttle=True)

        st = path.stat()
        if (st.st_size, st.st_mtime_ns) != (f.size, f.mtime_ns):
            with self._lock:
                j["files"].pop(rel, None)
            return None

        entry = FileEntry(
            size=f.size,
            mtime_ns=f.mtime_ns,
            sha256=hasher.hexdigest() if hasher else sha256_file(path),
            file_id=state["file_id"],
            nonce_prefix=state["nonce_prefix"],
            n=n,
            channel_id=channel_id,
            message_id=state["message_id"],
            thread_id=state["thread_id"],
            part_ids=[state["parts"][str(i)] for i in range(n)],
        )
        with self._lock:
            j["files"].pop(rel, None)
            j["done"][rel] = {**signature, "entry": asdict(entry)}
        self._finish(rel, entry, m, j)
        return entry

    def _finish(self, rel: str, entry: FileEntry, m: Manifest, j: dict) -> None:
        with self._lock:
            m.files[rel] = entry
            self._save(throttle=True)

    def _has_human_messages(self, channel_id: str) -> bool:
        try:
            msgs = self.client.recent_messages(channel_id, 100)
        except DiscordNotFound:
            return False
        return any(m.get("author", {}).get("id") != self.bot_id for m in msgs)

    def _delete_channel_if_ours(self, channel_id: str) -> bool:
        if channel_id in self._kept_parents or self._has_human_messages(channel_id):
            return False
        self._ignore_missing(self.client.delete_channel, channel_id)
        return True

    def _delete_file_objects(self, entry: FileEntry) -> None:
        if entry.thread_id and not self._delete_channel_if_ours(entry.thread_id):
            self._kept_parents.add(entry.channel_id)
            return
        self._ignore_missing(self.client.delete_message, entry.channel_id, entry.message_id)

    @staticmethod
    def _ignore_missing(fn, *args) -> None:
        try:
            fn(*args)
        except DiscordNotFound:
            pass

    def _apply_deletions(self, plan: Plan, m: Manifest) -> list[str]:
        deleted = [rel for rel in plan.deleted if m.files.pop(rel, None) is not None]
        used = {e.channel_id for e in m.files.values()}
        for key in plan.delete_channels:
            if m.channels.get(key) and m.channels[key] not in used:
                m.channels.pop(key)
        used_tops = {k.split("/", 1)[0] for k in m.channels}
        for top in plan.delete_categories:
            if top not in used_tops:
                m.categories.pop(top, None)
        return deleted

    def _record_version(self, m: Manifest, *, kind: str, deleted=(), untrash=frozenset()) -> history.Outcome:
        out = history.advance(
            self.manifest, m, kind=kind, at=datetime.now().isoformat(timespec="seconds"), deleted=deleted,
            untrash=untrash, keep_versions=self.keep_versions, keep_trash=self.keep_trash,
        )
        m.history, m.trash = out.manifest.history, out.manifest.trash
        m.retired_channels, m.retired_categories = out.manifest.retired_channels, out.manifest.retired_categories
        todo = self._journal()["to_delete"]
        todo += [{"kind": "entry", "entry": asdict(e)} for e in out.garbage.entries]
        folder_of = {cid: key for key, cid in self.manifest.channels.items()}
        todo += [{"kind": "channel", "key": folder_of.get(cid, tr("(지난 폴더)")), "id": cid} for cid in out.garbage.channels]
        todo += [{"kind": "category", "key": cid, "id": cid} for cid in out.garbage.categories]
        if out.dropped_stale_history:
            self._event(tr("예전 버전의 Secret으로 동기화한 적이 있어 버전 기록을 처음부터 다시 쌓습니다"), "warn")
        if out.trimmed_versions or out.trimmed_trash:
            self._event(tr("디스코드 채널 한도 때문에 오래된 버전 {trimmed_versions}개와 휴지통 파일 {trimmed_trash}개를 정리했습니다", trimmed_versions=out.trimmed_versions, trimmed_trash=out.trimmed_trash), "warn")
        self._save()
        return out

    def _live(self, m: Manifest) -> set[str]:
        live = set(m.categories.values()) | set(m.channels.values()) | set(m.retired_channels) | set(m.retired_categories)
        for e in history.references(m.files, m.history, m.trash).values():
            live |= {e.channel_id, e.message_id, *e.part_ids} | ({e.thread_id} if e.thread_id else set())
        return live

    def _run_deletes(self, m: Manifest, result: SyncResult) -> None:
        todo = self._journal()["to_delete"]
        if not todo:
            return
        live = self._live(m)
        items = [t for t in todo if t["kind"] in ("entry", "file", "old")]
        for t in items:
            entry = FileEntry(**t["entry"])
            if {entry.message_id, entry.thread_id} & live:
                continue
            self._delete_file_objects(entry)
        for t in (t for t in todo if t["kind"] == "channel"):
            if t["id"] not in live and not self._delete_channel_if_ours(t["id"]):
                result.kept_channels.append(t["key"])
                self._event(tr("직접 쓴 글이 있어 채널을 남겨 둠: {v1}", v1=t['key']), "warn")
        cats = [t for t in todo if t["kind"] == "category" and t["id"] not in live]
        remaining = self.client.channels(self.guild_id) if cats else []
        for t in cats:
            if not any(c.get("parent_id") == t["id"] for c in remaining):
                self._ignore_missing(self.client.delete_channel, t["id"])
        todo.clear()
        self._save()

    def state_at(self, version_id: int) -> Manifest:
        if self.manifest is None:
            self.load_manifest()
        return history.state_at(self.manifest, version_id)

    def commit_state(self, state: Manifest, *, kind: str, untrash=frozenset()) -> history.Outcome:
        if self.manifest is None:
            self.load_manifest()
        if self.index_channel is None:
            raise manifest_mod.ManifestError("no backup")
        m = Manifest.from_json(self.manifest.to_json())
        m.files, m.dirs = dict(state.files), list(state.dirs)
        m.channels, m.categories = dict(state.channels), dict(state.categories)
        out = self._record_version(m, kind=kind, untrash=untrash)
        m.touch()
        self._publish_index(m)
        self._run_deletes(m, SyncResult())
        self._cleanup(m)
        self.manifest = m
        return out

    def republish_index(self) -> bool:
        self.load_manifest()
        if self.index_channel is None or not self.index_messages:
            return False
        self._publish_index(self.manifest)
        return True

    def _publish_index(self, m: Manifest) -> None:
        header = keys.wrap(self.settings.dk, self.store.password)
        blob = manifest_mod.encode_index(m, self.settings.dk, header)
        if len(blob) <= self.part_size:
            msg = self.client.send_file(self.index_channel, f"{INDEX_PREFIX} v1", "index.bin", blob)
        else:
            pieces = [blob[i : i + self.part_size] for i in range(0, len(blob), self.part_size)]
            msg = self.client.send_message(self.index_channel, f"{INDEX_PREFIX} v1 parts={len(pieces)}")
            tid = self.client.create_thread(self.index_channel, msg["id"], _rand_name("t"))
            for i, piece in enumerate(pieces):
                self.client.send_file(tid, "", f"index.{i + 1:03d}", piece)
            msg["thread"] = {"id": tid}
        self.settings.index_message_id = msg["id"]
        self.settings.index_channel_id = self.index_channel
        self.settings.manifest = m.to_json()
        self._index_ids = {msg["id"]} | ({msg["thread"]["id"]} if msg.get("thread") else set())

        for old in self._broken_index + self.index_messages[INDEX_KEEP - 1 :]:
            if old.get("thread"):
                self._ignore_missing(self.client.delete_channel, old["thread"]["id"])
            self._ignore_missing(self.client.delete_message, self.index_channel, old["id"])
        self.index_messages = [msg] + self.index_messages[: INDEX_KEEP - 1]
        self._broken_index = []
        self._save()
        self._event(tr("목차를 저장했습니다 (파일 {n}개)", n=len(m.files)))

    def _cleanup(self, m: Manifest) -> None:
        referenced = {self.control_category, self.index_channel, *self._index_ids} | self._live(m)
        created = self._journal()["created"]
        threads = {c["id"] for c in created if c["kind"] == "thread" and c["id"] not in referenced}
        for c in created:
            if c["id"] in referenced:
                continue
            if c["kind"] in ("thread", "channel"):
                self._delete_channel_if_ours(c["id"])
            elif c["kind"] == "message" and c["channel"] not in threads:
                self._ignore_missing(self.client.delete_message, c["channel"], c["id"])
        self.settings.journal = {}
        self._save()


def download_message_blob(client, channel_id: str, message: dict) -> bytes:
    fresh = client.get_message(channel_id, message["id"])
    if fresh.get("attachments"):
        return client.download(fresh["attachments"][0]["url"])
    thread = fresh.get("thread") or message.get("thread")
    if not thread:
        raise DiscordError("message has no data")
    parts = [p for p in sorted(client.recent_messages(thread["id"], 100), key=lambda x: int(x["id"])) if p.get("attachments")]
    expected = re.search(r"parts=(\d+)", fresh.get("content") or message.get("content") or "")
    if expected and len(parts) != int(expected.group(1)):
        raise manifest_mod.ManifestError(tr("목차 조각이 모자랍니다 ({n}/{v1})", n=len(parts), v1=expected.group(1)))
    return b"".join(client.download(p["attachments"][0]["url"]) for p in parts)
