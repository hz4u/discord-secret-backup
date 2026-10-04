import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .crypto_core import SALT_LEN, derive_key, seal, unseal

DK_LEN = 32
DK_AAD = b"SECRET-DK1"
_HEADER_VERSION = 1


class KeyHeaderError(ValueError):
    pass


@dataclass(frozen=True)
class KeyHeader:
    pw_salt: bytes
    wrapped_dk: bytes

    def to_json(self) -> dict:
        return {
            "v": _HEADER_VERSION,
            "pw_salt": base64.b64encode(self.pw_salt).decode(),
            "wrapped_dk": base64.b64encode(self.wrapped_dk).decode(),
        }

    @classmethod
    def from_json(cls, data: dict) -> "KeyHeader":
        try:
            if data["v"] != _HEADER_VERSION:
                raise KeyHeaderError(f"unsupported key header version {data['v']}")
            return cls(base64.b64decode(data["pw_salt"]), base64.b64decode(data["wrapped_dk"]))
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, KeyHeaderError):
                raise
            raise KeyHeaderError("bad key header") from exc


def new_data_key() -> bytes:
    return os.urandom(DK_LEN)


def wrap(dk: bytes, password: str) -> KeyHeader:
    salt = os.urandom(SALT_LEN)
    return KeyHeader(salt, seal(derive_key(password, salt), dk, DK_AAD))


def unwrap(header: KeyHeader, password: str) -> bytes:
    return unseal(derive_key(password, header.pw_salt), header.wrapped_dk, DK_AAD)


def subkey(dk: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label).derive(dk)


def file_key(dk: bytes, file_id: bytes) -> bytes:
    return subkey(dk, b"SECRET-FILE1" + file_id)


def index_key(dk: bytes) -> bytes:
    return subkey(dk, b"SECRET-INDEX1")
