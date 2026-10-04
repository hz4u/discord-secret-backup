import ctypes
import sys
import threading
from ctypes import wintypes
from pathlib import Path

_IGNORE_ROOT = {".secret", "system volume information", "$recycle.bin"}
_IGNORE_SUFFIX = (".secret-part", ".secret-tmp")


def is_relevant(rel: str) -> bool:
    parts = rel.replace("\\", "/").strip("/").split("/")
    if not parts or not parts[0]:
        return False
    if parts[0].lower() in _IGNORE_ROOT:
        return False
    return not parts[-1].lower().endswith(_IGNORE_SUFFIX)


class FolderWatcher:
    BUFFER = 64 * 1024
    POLL_MS = 300

    def __init__(self, root: Path, on_change):
        self.root = Path(root)
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if sys.platform != "win32" or not self.root.is_dir():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="secret-folder-watch", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

        k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                              wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(OVERLAPPED), ctypes.c_void_p]
        k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
        k32.ResetEvent.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        invalid = wintypes.HANDLE(-1).value
        handle = k32.CreateFileW(
            str(self.root), 0x0001,
            0x1 | 0x2 | 0x4,
            None, 3,
            0x02000000 | 0x40000000,
            None,
        )
        if not handle or handle == invalid:
            return
        event = k32.CreateEventW(None, True, False, None)
        ov = OVERLAPPED()
        ov.hEvent = event
        buf = ctypes.create_string_buffer(self.BUFFER)
        notify = 0x1 | 0x2 | 0x8 | 0x10
        try:
            while not self._stop.is_set():
                k32.ResetEvent(event)
                if not k32.ReadDirectoryChangesW(handle, buf, self.BUFFER, True, notify, None, ctypes.byref(ov), None):
                    return
                while not self._stop.is_set():
                    if k32.WaitForSingleObject(event, self.POLL_MS) == 0:
                        break
                else:
                    k32.CancelIoEx(handle, ctypes.byref(ov))
                    return
                got = wintypes.DWORD(0)
                if not k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(got), False):
                    return
                if got.value == 0:
                    self.on_change()
                    continue
                if any(is_relevant(rel) for rel in _parse(buf.raw[: got.value])):
                    self.on_change()
        finally:
            k32.CloseHandle(event)
            k32.CloseHandle(handle)


def _parse(data: bytes) -> list[str]:
    out, offset = [], 0
    while offset + 12 <= len(data):
        next_offset, _action, length = (int.from_bytes(data[offset + i: offset + i + 4], "little") for i in (0, 4, 8))
        out.append(data[offset + 12: offset + 12 + length].decode("utf-16-le", errors="replace"))
        if not next_offset:
            break
        offset += next_offset
    return out
