import ctypes
import struct
import time
from ctypes import wintypes

from ..i18n import tr

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

PRIVACY_FORMATS = {
    "ExcludeClipboardContentFromMonitorProcessing": struct.pack("<I", 0),
    "CanIncludeInClipboardHistory": struct.pack("<I", 0),
    "CanUploadToCloudClipboard": struct.pack("<I", 0),
}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL


class _Clipboard:
    def __enter__(self):
        for _ in range(20):
            if _user32.OpenClipboard(None):
                return self
            time.sleep(0.02)
        raise OSError(tr("클립보드를 열 수 없습니다"))

    def __exit__(self, *exc):
        _user32.CloseClipboard()


def _set_data(fmt: int, data: bytes) -> None:
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise MemoryError
    ptr = _kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    _kernel32.GlobalUnlock(handle)
    if not _user32.SetClipboardData(fmt, handle):
        _kernel32.GlobalFree(handle)
        raise OSError(tr("클립보드에 쓸 수 없습니다"))


def set_text(text: str) -> None:
    with _Clipboard():
        _user32.EmptyClipboard()
        _set_data(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        for name, value in PRIVACY_FORMATS.items():
            _set_data(_user32.RegisterClipboardFormatW(name), value)


def get_text() -> str | None:
    with _Clipboard():
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)


def clear() -> None:
    with _Clipboard():
        _user32.EmptyClipboard()


def has_format(name: str) -> bool:
    return bool(_user32.IsClipboardFormatAvailable(_user32.RegisterClipboardFormatW(name)))
