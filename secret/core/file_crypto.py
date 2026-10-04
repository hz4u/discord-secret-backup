import os
import re
import secrets
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..i18n import tr
from .crypto_core import (
    NONCE_LEN,
    SALT_LEN,
    TAG_LEN,
    CryptoError,
    DecryptionError,
    FormatError,
    decrypt_blob,
    derive_key,
)

MAGIC = b"FILE1"
MAGIC2 = b"FILE2"
MAX_SIZE = 200 * 1024 * 1024
DISCORD_FILE_LIMIT = 10 * 1024 * 1024
IO_CHUNK = 4 * 1024 * 1024
TEMP_SUFFIX = ".secret-part"

_FILE_ID_LEN = 16
_PREFIX_LEN = 8
_HEADER2 = struct.Struct(f">{len(MAGIC2)}s{SALT_LEN}s{_FILE_ID_LEN}s{_PREFIX_LEN}sII")
_OVERHEAD = len(MAGIC) + SALT_LEN + NONCE_LEN + TAG_LEN + 2 + 1024
_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class FileTooLargeError(CryptoError):
    pass


def is_encrypted_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) in (MAGIC, MAGIC2)
    except OSError:
        return False


def safe_name(name: str) -> str:
    base = re.split(r"[\\/]", name)[-1]
    base = _INVALID_CHARS.sub("_", base).rstrip(" .")
    if not base or base in (".", ".."):
        return "restored_file"
    if base.split(".")[0].upper() in _RESERVED:
        base = "_" + base
    return base


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def _original_name(path: Path) -> str:
    return path.name


def _report(on_progress, frac: float) -> None:
    if on_progress:
        on_progress(min(1.0, frac))


def _chunk_crypto(header: bytes, prefix: bytes, index: int) -> tuple[bytes, bytes]:
    i = struct.pack(">I", index)
    return prefix + i, header + i


def _parse_name(first: bytes) -> tuple[str, int]:
    if len(first) < 2:
        raise FormatError("missing name header")
    (name_len,) = struct.unpack(">H", first[:2])
    if len(first) < 2 + name_len:
        raise FormatError("truncated name")
    try:
        return first[2 : 2 + name_len].decode("utf-8"), 2 + name_len
    except UnicodeDecodeError:
        raise FormatError("bad name encoding") from None


def encrypt_file(src: Path, password: str, on_progress=None) -> Path:
    size = src.stat().st_size
    if size > MAX_SIZE:
        raise FileTooLargeError(size)
    name = _original_name(src).encode("utf-8")[:0xFFFF]
    lead = struct.pack(">H", len(name)) + name
    chunk = max(IO_CHUNK, len(lead))
    plain_len = len(lead) + size
    total = max(1, -(-plain_len // chunk))
    salt, file_id, prefix = os.urandom(SALT_LEN), os.urandom(_FILE_ID_LEN), os.urandom(_PREFIX_LEN)
    header = _HEADER2.pack(MAGIC2, salt, file_id, prefix, chunk, total)
    aead = AESGCM(derive_key(password, salt))

    while (out := src.parent / f"{secrets.token_hex(4)}.enc").exists():
        pass
    try:
        with open(src, "rb") as fin, open(out, "xb") as fout:
            fout.write(header)
            pending = lead
            for i in range(total):
                piece = pending + fin.read(chunk - len(pending))
                pending = b""
                nonce, aad = _chunk_crypto(header, prefix, i)
                fout.write(aead.encrypt(nonce, piece, aad))
                _report(on_progress, (i + 1) / total)
            if fin.read(1):
                raise OSError(tr("파일이 암호화하는 동안 커졌습니다"))
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    _report(on_progress, 1.0)
    return out


def decrypt_file(src: Path, password: str, on_progress=None) -> Path:
    with open(src, "rb") as f:
        magic = f.read(len(MAGIC))
    if magic == MAGIC2:
        return _decrypt_file2(src, password, on_progress)
    if magic == MAGIC:
        return _decrypt_file1(src, password, on_progress)
    raise FormatError("not an encrypted file")


def _decrypt_file2(src: Path, password: str, on_progress) -> Path:
    file_size = src.stat().st_size
    with open(src, "rb") as fin:
        header = fin.read(_HEADER2.size)
        if len(header) < _HEADER2.size:
            raise FormatError("truncated header")
        _, salt, _file_id, prefix, chunk, total = _HEADER2.unpack(header)
        body = file_size - _HEADER2.size
        full = chunk + TAG_LEN
        last = body - (total - 1) * full
        if chunk < 2 or total < 1 or not TAG_LEN <= last <= full:
            raise FormatError("bad chunk layout")
        aead = AESGCM(derive_key(password, salt))
        tmp = src.parent / f".{secrets.token_hex(6)}{TEMP_SUFFIX}"
        try:
            with open(tmp, "xb") as fout:
                name = None
                for i in range(total):
                    ct = fin.read(full if i < total - 1 else last)
                    nonce, aad = _chunk_crypto(header, prefix, i)
                    try:
                        piece = aead.decrypt(nonce, ct, aad)
                    except InvalidTag:
                        raise DecryptionError("wrong password or tampered data") from None
                    if name is None:
                        name, start = _parse_name(piece)
                        piece = piece[start:]
                    fout.write(piece)
                    _report(on_progress, (i + 1) / total * 0.99)
            out = unique_path(src.parent, safe_name(name))
            os.replace(tmp, out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    _report(on_progress, 1.0)
    return out


def _decrypt_file1(src: Path, password: str, on_progress) -> Path:
    if src.stat().st_size > MAX_SIZE + _OVERHEAD:
        raise FileTooLargeError(src.stat().st_size)
    data = src.read_bytes()
    _report(on_progress, 0.4)
    plaintext = decrypt_blob(password, data[len(MAGIC):], MAGIC)
    _report(on_progress, 0.7)
    name, start = _parse_name(plaintext)
    out = unique_path(src.parent, safe_name(name))
    try:
        with open(out, "xb") as f:
            f.write(memoryview(plaintext)[start:])
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    _report(on_progress, 1.0)
    return out
