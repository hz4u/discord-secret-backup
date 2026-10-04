import ctypes
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from ..i18n import tr

DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE = 2, 3, 4
BUS_USB, BUS_SD, BUS_MMC, BUS_VIRTUAL, BUS_FILE_VIRTUAL = 7, 12, 13, 14, 15

IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
PROPERTY_DEVICE = 0
PROPERTY_SEEK_PENALTY = 7
PROPERTY_TRIM = 8
BUS_TYPE_OFFSET = 28
FLAG_OFFSET = 8


@dataclass(frozen=True)
class DriveKind:
    name: str
    title: str
    icon: str


USB = DriveKind("USB", tr("USB 드라이브"), "hard-drives")
EXTERNAL_HDD = DriveKind(tr("외장 HDD"), tr("외장 HDD"), "hard-drive")
EXTERNAL_SSD = DriveKind(tr("외장 SSD"), tr("외장 SSD"), "hard-drive")
EXTERNAL = DriveKind(tr("외장 드라이브"), tr("외장 드라이브"), "hard-drive")
SD_CARD = DriveKind(tr("SD 카드"), tr("SD 카드"), "sim-card")
NETWORK = DriveKind(tr("네트워크 드라이브"), tr("네트워크 드라이브"), "share-network")
VIRTUAL = DriveKind(tr("가상 디스크"), tr("가상 디스크"), "hard-drives")
INTERNAL_HDD = DriveKind("HDD", tr("내장 HDD"), "hard-drive")
INTERNAL_SSD = DriveKind("SSD", tr("내장 SSD"), "hard-drive")
INTERNAL = DriveKind(tr("디스크"), tr("내장 디스크"), "hard-drive")


def _spins(seek_penalty: bool | None, trim: bool | None) -> bool | None:
    if seek_penalty is not None:
        return seek_penalty
    if trim is None:
        return None
    return not trim


def classify(drive_type: int | None, bus: int | None, seek_penalty: bool | None, trim: bool | None = None) -> DriveKind:
    if drive_type == DRIVE_REMOTE:
        return NETWORK
    if bus in (BUS_SD, BUS_MMC):
        return SD_CARD
    if bus in (BUS_VIRTUAL, BUS_FILE_VIRTUAL):
        return VIRTUAL
    if bus == BUS_USB:
        if drive_type == DRIVE_REMOVABLE:
            return USB
        return {True: EXTERNAL_HDD, False: EXTERNAL_SSD}.get(_spins(seek_penalty, trim), EXTERNAL)
    if drive_type == DRIVE_REMOVABLE:
        return USB
    if bus is None:
        return USB
    return {True: INTERNAL_HDD, False: INTERNAL_SSD}.get(_spins(seek_penalty, trim), INTERNAL)


def _query(handle, property_id: int, offset: int, fmt: str):
    kernel32 = ctypes.windll.kernel32
    query = struct.pack("<II4x", property_id, 0)
    out = ctypes.create_string_buffer(1024)
    got = ctypes.c_uint32()
    ok = kernel32.DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY, query, len(query), out, len(out), ctypes.byref(got), None)
    if not ok or got.value < offset + struct.calcsize(fmt):
        return None
    return struct.unpack_from(fmt, out.raw, offset)[0]


def detect(root: Path) -> DriveKind:
    if sys.platform != "win32":
        return USB
    kernel32 = ctypes.windll.kernel32
    anchor = Path(root).anchor
    drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor))
    bus = seek = trim = None
    if drive_type != DRIVE_REMOTE and len(anchor) >= 2 and anchor[1] == ":":
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(ctypes.c_wchar_p(f"\\\\.\\{anchor[:2]}"), 0, 3, None, 3, 0, None)
        if handle not in (None, ctypes.c_void_p(-1).value):
            try:
                bus = _query(ctypes.c_void_p(handle), PROPERTY_DEVICE, BUS_TYPE_OFFSET, "<I")
                penalty = _query(ctypes.c_void_p(handle), PROPERTY_SEEK_PENALTY, FLAG_OFFSET, "<B")
                seek = None if penalty is None else bool(penalty)
                trimmed = _query(ctypes.c_void_p(handle), PROPERTY_TRIM, FLAG_OFFSET, "<B")
                trim = None if trimmed is None else bool(trimmed)
            finally:
                kernel32.CloseHandle(ctypes.c_void_p(handle))
    return classify(drive_type, bus, seek, trim)
