import os
from dataclasses import dataclass, field
from pathlib import Path

KINDS: dict[str, str] = {
    **dict.fromkeys((
        "jpg", "jpeg", "jfif", "png", "gif", "webp", "bmp", "heic", "heif", "avif", "tif", "tiff", "svg", "ico",
        "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf", "srw", "psd",
    ), "image"),
    **dict.fromkeys(("mp4", "m4v", "mov", "mkv", "avi", "webm", "wmv", "flv", "3gp", "mpg", "mpeg", "mts", "m2ts", "vob"), "video"),
    **dict.fromkeys((
        "txt", "md", "pdf", "hwp", "hwpx", "doc", "docx", "rtf", "odt", "ppt", "pptx", "odp", "key",
        "xlsx", "xls", "csv", "ods", "numbers", "pages", "epub", "json", "xml", "html", "htm", "log",
    ), "document"),
    **dict.fromkeys(("mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "wma", "aiff", "aif", "mid", "midi"), "music"),
}
OTHER = "other"
ARCHIVE_EXTS = {"zip", "7z", "rar", "tar", "gz", "bz2", "xz", "alz", "egg"}

SKIP_EXTS = {"tmp", "temp", "crdownload", "part", "partial", "download", "opdownload", "!ut", "secret-part"}
SKIP_NAMES = {"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"}

_HIDDEN_AT_ROOT = {"secret.exe", ".secret", "system volume information", "$recycle.bin"}
_SKIP_ATTRIBUTES = 0x2 | 0x4


@dataclass(frozen=True)
class ScannedFile:
    rel: str
    size: int
    mtime_ns: int
    kind: str | None


@dataclass
class ScanResult:
    files: dict[str, ScannedFile] = field(default_factory=dict)
    excluded: dict[str, ScannedFile] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def kind_of(name: str) -> str | None:
    lower = name.lower()
    stem, dot, ext = lower.rpartition(".")
    if lower in SKIP_NAMES or lower.startswith("~$") or (dot and ext in SKIP_EXTS):
        return None
    if not dot or not stem:
        return OTHER
    return KINDS.get(ext, OTHER)


def is_hidden_root_name(name: str) -> bool:
    return name.lower() in _HIDDEN_AT_ROOT


def logical_folder(rel: str) -> tuple[str, str]:
    parts = rel.split("/")
    if len(parts) == 1:
        return "", ""
    if len(parts) == 2:
        return parts[0], ""
    return parts[0], parts[1]


def channel_key(top: str, sub: str) -> str:
    return f"{top}/{sub}"


def scan(root: Path, include_hidden=()) -> ScanResult:
    result = ScanResult()
    include = set(include_hidden)

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            result.unreadable.append(prefix.rstrip("/") or ".")
            return
        for entry in entries:
            if not prefix and is_hidden_root_name(entry.name):
                continue
            rel = prefix + entry.name
            try:
                st = entry.stat(follow_symlinks=False)
                hidden_by_secret = rel in include
                if getattr(st, "st_file_attributes", 0) & _SKIP_ATTRIBUTES and not hidden_by_secret:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    result.dirs.append(rel)
                    walk(Path(entry.path), rel + "/")
                elif entry.is_file(follow_symlinks=False):
                    item = ScannedFile(rel, st.st_size, st.st_mtime_ns, kind_of(entry.name))
                    (result.files if item.kind else result.excluded)[rel] = item
            except OSError:
                result.unreadable.append(rel)

    walk(root, "")
    return result
