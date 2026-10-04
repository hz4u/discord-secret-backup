import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KDF_N = 2**17
KDF_R = 8
KDF_P = 1
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16

DISCORD_LIMIT = 2000

TEXT_VERSION = "1"
TEXT_AAD = b"ENC1"
_TOKEN_RE = re.compile(r"ENC(\d+):([A-Za-z0-9+/=\s]*)")


class CryptoError(Exception):
    pass


class DecryptionError(CryptoError):
    pass


class FormatError(CryptoError):
    pass


class UnsupportedVersionError(CryptoError):
    pass


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=KDF_N, r=KDF_R, p=KDF_P)
    return kdf.derive(password.encode("utf-8"))


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, data: bytes, aad: bytes) -> bytes:
    if len(data) < NONCE_LEN + TAG_LEN:
        raise FormatError("data too short")
    nonce, ciphertext = data[:NONCE_LEN], data[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        raise DecryptionError("wrong password or tampered data") from None


def encrypt_blob(password: str, plaintext: bytes, aad: bytes) -> bytes:
    salt = os.urandom(SALT_LEN)
    return salt + seal(derive_key(password, salt), plaintext, aad)


def decrypt_blob(password: str, body: bytes, aad: bytes) -> bytes:
    if len(body) < SALT_LEN + NONCE_LEN + TAG_LEN:
        raise FormatError("data too short")
    salt = body[:SALT_LEN]
    return unseal(derive_key(password, salt), body[SALT_LEN:], aad)


def clean_token(raw: str) -> str:
    match = _TOKEN_RE.search(raw)
    if not match:
        raise FormatError("no ENC token found")
    version, payload = match.group(1), re.sub(r"\s+", "", match.group(2))
    return f"ENC{version}:{payload}"


def encrypt_text(text: str, password: str) -> str:
    body = encrypt_blob(password, text.encode("utf-8"), TEXT_AAD)
    return f"ENC{TEXT_VERSION}:" + base64.b64encode(body).decode("ascii")


def decrypt_text(token: str, password: str) -> str:
    cleaned = clean_token(token)
    version, payload = cleaned[3:].split(":", 1)
    if version != TEXT_VERSION:
        raise UnsupportedVersionError(version)
    try:
        body = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise FormatError("invalid base64") from None
    return decrypt_blob(password, body, TEXT_AAD).decode("utf-8")
