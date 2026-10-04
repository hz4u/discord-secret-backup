import copy
import hashlib
from dataclasses import dataclass, field

from .manifest import FileEntry, Manifest, TrashItem, Version

KEEP_VERSIONS = 30
KEEP_TRASH = 100
CONTROL_CHANNELS = 2
CHANNEL_BUDGET = 450


@dataclass
class Garbage:
    entries: list[FileEntry] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    manifest: Manifest
    garbage: Garbage
    version: Version | None
    trimmed_versions: int = 0
    trimmed_trash: int = 0
    dropped_stale_history: bool = False


def state_digest(files: dict[str, FileEntry]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel].message_id}\n".encode("utf-8"))
    return h.hexdigest()[:24]


def references(files: dict[str, FileEntry], versions: list[Version], trash: list[TrashItem]) -> dict[str, FileEntry]:
    refs = {e.message_id: e for e in files.values()}
    for v in versions[1:]:
        for before, _ in v.changes.values():
            if before is not None:
                refs.setdefault(before.message_id, before)
    for t in trash:
        refs.setdefault(t.entry.message_id, t.entry)
    return refs


def _diff(old: dict[str, FileEntry], new: dict[str, FileEntry]) -> tuple[dict, dict]:
    changes = {}
    for rel in old.keys() | new.keys():
        before, after = old.get(rel), new.get(rel)
        if (before and before.message_id) != (after and after.message_id):
            changes[rel] = (before, after)
    moved_ids = {a.message_id for b, a in changes.values() if a is not None and b is None} & {
        b.message_id for b, a in changes.values() if b is not None and a is None}
    summary = {
        "new": sum(1 for b, a in changes.values() if b is None and a.message_id not in moved_ids),
        "changed": sum(1 for b, a in changes.values() if b is not None and a is not None),
        "deleted": sum(1 for b, a in changes.values() if a is None and b.message_id not in moved_ids),
        "renamed": len(moved_ids),
    }
    return changes, summary


def _prune_trash(trash: list[TrashItem], keep: int) -> list[TrashItem]:
    if keep <= 0:
        return []
    batches: dict[int, list[TrashItem]] = {}
    for t in trash:
        batches.setdefault(t.batch, []).append(t)
    kept: list[TrashItem] = []
    for batch in sorted(batches, reverse=True):
        if len(kept) >= keep:
            break
        kept = batches[batch] + kept
    return kept


def _category_of(cid: str, m: Manifest) -> str | None:
    if cid in m.retired_channels:
        return m.retired_channels[cid]
    for key, value in m.channels.items():
        if value == cid:
            return m.categories.get(key.split("/", 1)[0])
    return None


def advance(
    old: Manifest,
    new: Manifest,
    *,
    kind: str,
    at: str,
    deleted=(),
    untrash=frozenset(),
    keep_versions: int = KEEP_VERSIONS,
    keep_trash: int = KEEP_TRASH,
    channel_budget: int = CHANNEL_BUDGET,
) -> Outcome:
    result = copy.deepcopy(new)
    history = list(old.history)
    stale = bool(history) and history[-1].digest != state_digest(old.files)
    if stale:
        history = []

    changes, summary = _diff(old.files, new.files)
    version = None
    if changes or kind in ("sync", "rollback"):
        vid = (history[-1].id if history else max((v.id for v in old.history), default=0)) + 1
        version = Version(id=vid, at=at, kind=kind, summary=summary, changes=changes, dirs=list(new.dirs),
                          channels=dict(new.channels), categories=dict(new.categories), digest=state_digest(new.files))
        history.append(version)

    batch = version.id if version else 0
    live_shas = {e.sha256 for e in new.files.values()}
    trash = [t for t in old.trash if t.entry.message_id not in untrash]
    for rel in deleted:
        entry = old.files.get(rel)
        if entry is not None and entry.sha256 not in live_shas and rel not in new.files:
            trash.append(TrashItem(rel=rel, entry=entry, at=at, batch=batch))
    trash = _prune_trash(trash, keep_trash)
    history = history[-keep_versions:] if keep_versions > 0 else []

    old_refs = references(old.files, old.history, old.trash)
    trimmed_versions = trimmed_trash = 0
    while True:
        refs = references(new.files, history, trash)
        used_channels = {e.channel_id for e in refs.values()}
        live_channels = set(new.channels.values())
        retired = {}
        dead_channels = []
        for cid in (set(old.channels.values()) | set(old.retired_channels)) - live_channels:
            if cid in used_channels:
                retired[cid] = _category_of(cid, old)
            else:
                dead_channels.append(cid)
        live_categories = set(new.categories.values())
        retired_categories = sorted({c for c in retired.values() if c and c not in live_categories})
        dead_categories = sorted((set(old.categories.values()) | set(old.retired_categories))
                                 - live_categories - set(retired_categories))
        count = len(new.channels) + len(new.categories) + len(retired) + len(retired_categories) + CONTROL_CHANNELS
        if count <= channel_budget or not (history or trash):
            break
        if history:
            history = history[1:]
            trimmed_versions += 1
        else:
            oldest = min(t.batch for t in trash)
            trimmed_trash += sum(1 for t in trash if t.batch == oldest)
            trash = [t for t in trash if t.batch != oldest]

    result.history = history
    result.trash = trash
    result.retired_channels = retired
    result.retired_categories = retired_categories
    garbage = Garbage(
        entries=[e for mid, e in old_refs.items() if mid not in refs],
        channels=sorted(dead_channels),
        categories=dead_categories,
    )
    return Outcome(result, garbage, version, trimmed_versions, trimmed_trash, stale)


def state_at(m: Manifest, version_id: int) -> Manifest:
    files = dict(m.files)
    for v in reversed(m.history):
        if v.id == version_id:
            out = Manifest(categories=dict(v.categories), channels=dict(v.channels), files=files, dirs=list(v.dirs))
            return out
        for rel, (before, _) in v.changes.items():
            if before is None:
                files.pop(rel, None)
            else:
                files[rel] = before
    raise KeyError(version_id)
