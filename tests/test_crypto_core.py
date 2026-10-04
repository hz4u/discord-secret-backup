import base64

import pytest

from secret.core import crypto_core
from secret.core.crypto_core import (
    DecryptionError,
    FormatError,
    UnsupportedVersionError,
    decrypt_text,
    encrypt_text,
)

PW = "correct horse battery staple"


@pytest.mark.parametrize(
    "text",
    ["hello", "구글 복구키: abcd-efgh-1234", "🔑 이모지 😀", "x" * 5000, ""],
)
def test_text_roundtrip(text):
    token = encrypt_text(text, PW)
    assert token.startswith("ENC1:")
    assert decrypt_text(token, PW) == text


def test_wrong_password_fails():
    token = encrypt_text("secret", PW)
    with pytest.raises(DecryptionError):
        decrypt_text(token, PW + "x")


def test_tampered_token_fails():
    token = encrypt_text("secret", PW)
    raw = bytearray(base64.b64decode(token[5:]))
    raw[-1] ^= 0x01
    tampered = "ENC1:" + base64.b64encode(bytes(raw)).decode()
    with pytest.raises(DecryptionError):
        decrypt_text(tampered, PW)


def test_same_input_gives_different_output():
    assert encrypt_text("same", PW) != encrypt_text("same", PW)


@pytest.mark.parametrize(
    "wrap",
    [
        lambda t: f"  {t}  \n",
        lambda t: f"```\n{t}\n```",
        lambda t: f"```text\n{t}\n```",
        lambda t: f"`{t}`",
        lambda t: t[:30] + "\n" + t[30:60] + " " + t[60:],
    ],
)
def test_decrypt_tolerates_discord_formatting(wrap):
    token = encrypt_text("비밀", PW)
    assert decrypt_text(wrap(token), PW) == "비밀"


@pytest.mark.parametrize(
    "bad",
    ["", "hello", "ENC1:", "ENC1:!!!not base64!!!", "ENC1:" + base64.b64encode(b"short").decode()],
)
def test_malformed_token_raises_format_error(bad):
    with pytest.raises(FormatError):
        decrypt_text(bad, PW)


def test_unknown_version_raises():
    token = encrypt_text("x", PW)
    with pytest.raises(UnsupportedVersionError):
        decrypt_text("ENC2:" + token[5:], PW)


def test_aad_binds_format():
    body = crypto_core.encrypt_blob(PW, b"data", b"ENC1")
    with pytest.raises(DecryptionError):
        crypto_core.decrypt_blob(PW, body, b"FILE1")


def test_seal_unseal_with_key():
    key = crypto_core.derive_key(PW, b"s" * crypto_core.SALT_LEN)
    data = crypto_core.seal(key, b"payload", b"AAD")
    assert crypto_core.unseal(key, data, b"AAD") == b"payload"
    with pytest.raises(DecryptionError):
        crypto_core.unseal(key, data, b"OTHER")


@pytest.mark.real_kdf
def test_roundtrip_with_production_kdf():
    assert crypto_core.KDF_N == 2**17
    token = encrypt_text("real", PW)
    assert decrypt_text(token, PW) == "real"
