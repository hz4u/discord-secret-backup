import hashlib
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HEAD_BYTES = 64 * 1024
CHUNK = 1024 * 1024


class _Cancelled(Exception):
    pass


@dataclass
class DuplicateGroup:
    size: int
    files: list[str]

    @property
    def wasted(self) -> int:
        return self.size * (len(self.files) - 1)


@dataclass
class DuplicateResult:
    groups: list[DuplicateGroup] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def wasted(self) -> int:
        return sum(g.wasted for g in self.groups)


def _digest(path: Path, limit: int | None, cancel: threading.Event, on_bytes) -> bytes:
    h = hashlib.sha256()
    left = limit
    with open(path, "rb") as f:
        while left is None or left > 0:
            if cancel.is_set():
                raise _Cancelled
            chunk = f.read(CHUNK if left is None else min(CHUNK, left))
            if not chunk:
                break
            h.update(chunk)
            on_bytes(len(chunk))
            if left is not None:
                left -= len(chunk)
    return h.digest()


def find_duplicates(root: Path, files: dict, cancel: threading.Event | None = None, on_progress=None) -> DuplicateResult:
    cancel = cancel or threading.Event()
    result = DuplicateResult()
    by_size: dict[int, list[str]] = defaultdict(list)
    for rel, f in files.items():
        if f.size > 0:
            by_size[f.size].append(rel)
    candidates = [rels for rels in by_size.values() if len(rels) > 1]
    total = sum(min(files[r].size, HEAD_BYTES) for rels in candidates for r in rels)
    done = 0

    def report(current: str):
        def add(n: int):
            nonlocal done
            done += n
            if on_progress:
                on_progress(done, max(total, done), current)
        return add

    def group_by(rels: list[str], limit: int | None) -> list[list[str]]:
        seen: dict[bytes, list[str]] = defaultdict(list)
        for rel in rels:
            try:
                seen[_digest(root / rel, limit, cancel, report(rel))].append(rel)
            except OSError:
                result.unreadable.append(rel)
        return [g for g in seen.values() if len(g) > 1]

    try:
        for rels in candidates:
            for same_head in group_by(rels, HEAD_BYTES):
                size = files[same_head[0]].size
                if size <= HEAD_BYTES:
                    same = [same_head]
                else:
                    total += size * len(same_head)
                    same = group_by(same_head, None)
                for g in same:
                    g.sort(key=lambda r: (files[r].mtime_ns, r))
                    result.groups.append(DuplicateGroup(size, g))
    except _Cancelled:
        result.cancelled = True
    result.groups.sort(key=lambda g: (-g.wasted, g.files[0]))
    return result
