import ctypes
import sys
from pathlib import Path

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
_HIDE = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
_INVALID = 0xFFFFFFFF


def _attributes(path: Path) -> int | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.GetFileAttributesW.restype = ctypes.c_uint32
    attrs = kernel32.GetFileAttributesW(str(path))
    return None if attrs == _INVALID else attrs


def is_hidden_or_system(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    attrs = _attributes(path)
    return bool(attrs and attrs & _HIDE)


def set_folder_hidden(path: Path, hidden: bool) -> None:
    if sys.platform != "win32":
        return
    attrs = _attributes(path)
    if attrs is None:
        raise FileNotFoundError(str(path))
    new = attrs | _HIDE if hidden else attrs & ~_HIDE
    if not ctypes.windll.kernel32.SetFileAttributesW(str(path), new):
        raise ctypes.WinError()


def hide(path: Path) -> None:
    if sys.platform != "win32":
        return
    attrs = _attributes(path)
    if attrs is None or attrs & FILE_ATTRIBUTE_HIDDEN:
        return
    ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)


def ensure_hidden_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    hide(path)
    return path
