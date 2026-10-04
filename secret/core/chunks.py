import math
import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto_core import DecryptionError

PART_AAD = b"SECRET-PART1"
FILE_ID_LEN = 16
NONCE_PREFIX_LEN = 8
MARGIN = 64 * 1024
_MiB = 1024 * 1024
_TIER_LIMITS = {0: 10 * _MiB, 1: 10 * _MiB, 2: 50 * _MiB, 3: 100 * _MiB}


def upload_limit_for_tier(tier: int) -> int:
    return _TIER_LIMITS.get(tier, 10 * _MiB)


def part_size_for_limit(limit: int) -> int:
    return limit - MARGIN


def part_count(size: int, part_size: int) -> int:
    return max(1, math.ceil(size / part_size))


def new_file_id() -> bytes:
    return os.urandom(FILE_ID_LEN)


def new_nonce_prefix() -> bytes:
    return os.urandom(NONCE_PREFIX_LEN)


def _nonce_aad(file_id: bytes, prefix: bytes, index: int, total: int) -> tuple[bytes, bytes]:
    return prefix + struct.pack(">I", index), PART_AAD + file_id + struct.pack(">II", index, total)


def encrypt_part(key: bytes, file_id: bytes, prefix: bytes, index: int, total: int, data: bytes) -> bytes:
    nonce, aad = _nonce_aad(file_id, prefix, index, total)
    return AESGCM(key).encrypt(nonce, data, aad)


def decrypt_part(key: bytes, file_id: bytes, prefix: bytes, index: int, total: int, data: bytes) -> bytes:
    nonce, aad = _nonce_aad(file_id, prefix, index, total)
    try:
        return AESGCM(key).decrypt(nonce, data, aad)
    except InvalidTag:
        raise DecryptionError("part authentication failed") from None


def read_part(path: Path, index: int, part_size: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(index * part_size)
        return f.read(part_size)
