import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import Manifest
from .scanner import ScanResult, channel_key, logical_folder

CONTROL_CHANNELS = 2
CHANNEL_WARN = 450
CHANNEL_LIMIT = 500


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _channel_of(rel: str) -> str:
    return channel_key(*logical_folder(rel))


def _top(key: str) -> str:
    return key.split("/", 1)[0]


def _name(rel: str) -> str:
    return rel.rpartition("/")[2]


def cannot_see(scan: ScanResult):
    roots = set(scan.unreadable)
    if "." in roots:
        return lambda rel: True
    prefixes = tuple(u + "/" for u in roots)
    return lambda rel: rel in roots or rel.startswith(prefixes)


def _clear_winners(votes: Counter) -> list[tuple[str, str]]:
    out = []
    for (new, old), n in votes.most_common():
        rivals = [m for (a, b), m in votes.items() if (a, b) != (new, old) and (a == new or b == old)]
        if all(m < n for m in rivals):
            out.append((new, old))
    return out


def _ancestors(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


@dataclass
class Plan:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    create_categories: list[str] = field(default_factory=list)
    create_channels: list[str] = field(default_factory=list)
    delete_channels: list[str] = field(default_factory=list)
    delete_categories: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    dirs_changed: bool = False
    hashes: dict[str, str] = field(default_factory=dict)
    upload_bytes: int = 0
    channel_count_after: int = 0
    remap_channels: dict[str, str] = field(default_factory=dict)
    remap_categories: dict[str, str] = field(default_factory=dict)
    unseen: list[str] = field(default_factory=list)

    @property
    def uploads(self) -> list[str]:
        return self.new + self.changed

    @property
    def is_empty(self) -> bool:
        return not (
            self.new or self.changed or self.renamed or self.touched or self.deleted
            or self.create_channels or self.delete_channels or self.create_categories
            or self.delete_categories or self.dirs_changed or self.remap_channels or self.remap_categories
        )

    @property
    def renamed_folders(self) -> int:
        return len(self.remap_categories) + sum(1 for new, old in self.remap_channels.items() if _top(new) == _top(old))

    @property
    def over_limit(self) -> bool:
        return self.channel_count_after > CHANNEL_LIMIT

    @property
    def near_limit(self) -> bool:
        return self.channel_count_after > CHANNEL_WARN


def make_plan(manifest: Manifest, scan: ScanResult, root: Path, hasher=sha256_file, force=frozenset()) -> Plan:
    unseen = cannot_see(scan)
    plan = Plan(excluded=sorted(scan.excluded), dirs=sorted(set(scan.dirs) | {d for d in manifest.dirs if unseen(d)}))
    plan.dirs_changed = set(plan.dirs) != set(manifest.dirs)
    plan.unseen = sorted(rel for rel in manifest.files if rel not in scan.files and unseen(rel))

    new_candidates = []
    for rel, f in sorted(scan.files.items()):
        entry = manifest.files.get(rel)
        if entry is None:
            new_candidates.append(rel)
        elif rel in force or entry.size != f.size:
            plan.changed.append(rel)
        elif entry.mtime_ns != f.mtime_ns:
            digest = plan.hashes[rel] = hasher(root / rel)
            (plan.touched if digest == entry.sha256 else plan.changed).append(rel)

    gone = [rel for rel in sorted(manifest.files) if rel not in scan.files and not unseen(rel)]
    needed_channels = {_channel_of(rel) for rel in scan.files} | {_channel_of(rel) for rel in plan.unseen}
    needed_categories = {_top(key) for key in needed_channels}

    def digest(rel: str) -> str:
        if rel not in plan.hashes:
            plan.hashes[rel] = hasher(root / rel)
        return plan.hashes[rel]

    def same_file(new_rel: str, old_rel: str) -> bool:
        f, e = scan.files[new_rel], manifest.files[old_rel]
        if f.size != e.size:
            return False
        if _name(new_rel) == _name(old_rel) and f.mtime_ns == e.mtime_ns:
            return True
        return digest(new_rel) == e.sha256

    new_keys = needed_channels - set(manifest.channels)
    old_keys = set(manifest.channels) - needed_channels
    remap_ch: dict[str, str] = {}
    remap_cat: dict[str, str] = {}
    if new_keys and old_keys:
        by_name: dict[tuple[str, int], list[str]] = defaultdict(list)
        for rel in gone:
            if _channel_of(rel) in old_keys:
                by_name[(_name(rel), manifest.files[rel].size)].append(rel)
        votes: Counter = Counter()
        for rel in new_candidates:
            key = _channel_of(rel)
            if key in new_keys:
                olds = {_channel_of(o) for o in by_name.get((_name(rel), scan.files[rel].size), ()) if same_file(rel, o)}
                for ok in olds:
                    votes[(key, ok)] += 1
        cat_votes: Counter = Counter()
        for (nk, ok), n in votes.items():
            nt, ot = _top(nk), _top(ok)
            if nt != ot and nt not in manifest.categories and ot not in needed_categories:
                cat_votes[(nt, ot)] += n
        for nt, ot in _clear_winners(cat_votes):
            if nt not in remap_cat and ot not in remap_cat.values():
                remap_cat[nt] = ot
        for nk, ok in _clear_winners(votes):
            if nk in remap_ch or ok in remap_ch.values():
                continue
            if _top(nk) == _top(ok) or remap_cat.get(_top(nk)) == _top(ok):
                remap_ch[nk] = ok
    plan.remap_channels, plan.remap_categories = remap_ch, remap_cat
    after = {ok: nk for nk, ok in remap_ch.items()}

    by_key: dict[tuple[str, int], list[str]] = defaultdict(list)
    for rel in gone:
        key = _channel_of(rel)
        by_key[(after.get(key, key), manifest.files[rel].size)].append(rel)
    for rel in new_candidates:
        candidates = by_key.get((_channel_of(rel), scan.files[rel].size))
        match = None
        if candidates:
            f = scan.files[rel]
            match = next((o for o in candidates if _name(o) == _name(rel) and manifest.files[o].mtime_ns == f.mtime_ns), None)
            if match is None:
                match = next((o for o in candidates if manifest.files[o].sha256 == digest(rel)), None)
        if match:
            candidates.remove(match)
            gone.remove(match)
            plan.renamed.append((match, rel))
        else:
            plan.new.append(rel)
    plan.deleted = gone

    plan.create_channels = sorted(needed_channels - set(manifest.channels) - set(remap_ch))
    plan.delete_channels = sorted(set(manifest.channels) - needed_channels - set(remap_ch.values()))
    plan.create_categories = sorted(needed_categories - set(manifest.categories) - set(remap_cat))
    plan.delete_categories = sorted(set(manifest.categories) - needed_categories - set(remap_cat.values()))
    plan.channel_count_after = (len(needed_channels) + len(needed_categories) + CONTROL_CHANNELS
                                + len(manifest.retired_channels) + len(manifest.retired_categories))
    plan.upload_bytes = sum(scan.files[rel].size for rel in plan.uploads)
    return plan


@dataclass
class Pending:
    new: int = 0
    changed: int = 0
    deleted: int = 0
    excluded: int = 0
    per_folder: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return self.new + self.changed + self.deleted


def quick_pending(manifest: Manifest, scan: ScanResult, force=frozenset()) -> Pending:
    pending = Pending(excluded=len(scan.excluded))
    unseen = cannot_see(scan)
    changed_paths = []
    moved: dict[tuple[str, int, int], list[str]] = defaultdict(list)
    for rel, e in manifest.files.items():
        if rel not in scan.files:
            moved[(_name(rel), e.size, e.mtime_ns)].append(rel)
    matched_gone = set()
    for rel, f in scan.files.items():
        entry = manifest.files.get(rel)
        if entry is None and moved.get((_name(rel), f.size, f.mtime_ns)):
            matched_gone.add(moved[(_name(rel), f.size, f.mtime_ns)].pop())
            continue
        if entry is None:
            pending.new += 1
            changed_paths.append(rel)
        elif rel in force or (entry.size, entry.mtime_ns) != (f.size, f.mtime_ns):
            pending.changed += 1
            changed_paths.append(rel)
    for rel in manifest.files:
        if rel not in scan.files and rel not in matched_gone and not unseen(rel):
            pending.deleted += 1
            changed_paths.append(rel)
    for rel in changed_paths:
        pending.per_folder.update(_ancestors(rel))
    return pending
