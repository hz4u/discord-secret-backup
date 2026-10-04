import os
from pathlib import Path

from ..core import textfile
from .decode import HEIC_EXTS, PDF_EXTS, QT_IMAGE_EXTS
from .video_thumbs import AUDIO_EXTS, VIDEO_EXTS

IMAGE_EXTS = QT_IMAGE_EXTS | HEIC_EXTS
ARCHIVE_EXTS = {"zip"}
MEMORY_KINDS = {"image", "pdf", "archive"}


def ext_of(rel: str) -> str:
    return rel.rpartition(".")[2].lower() if "." in rel.rpartition("/")[2] else ""


def viewer_kind(rel: str) -> str | None:
    ext = ext_of(rel)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in textfile.EDITABLE_EXTS:
        return "text"
    return None


def open_external(path: Path) -> None:
    os.startfile(path)  # noqa: S606
