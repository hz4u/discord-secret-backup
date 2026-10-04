import pytest

from secret.core import keys
from secret.core.crypto_core import DecryptionError

PW = "central passphrase for secret"


def test_wrap_unwrap_roundtrip():
    dk = keys.new_data_key()
    assert len(dk) == 32
    header = keys.wrap(dk, PW)
    assert keys.unwrap(header, PW) == dk


def test_wrong_password():
    header = keys.wrap(keys.new_data_key(), PW)
    with pytest.raises(DecryptionError):
        keys.unwrap(header, "nope")


def test_header_json_roundtrip():
    dk = keys.new_data_key()
    header = keys.wrap(dk, PW)
    restored = keys.KeyHeader.from_json(header.to_json())
    assert restored == header
    assert keys.unwrap(restored, PW) == dk


def test_each_wrap_uses_new_salt():
    dk = keys.new_data_key()
    a, b = keys.wrap(dk, PW), keys.wrap(dk, PW)
    assert a.pw_salt != b.pw_salt
    assert a.wrapped_dk != b.wrapped_dk


def test_password_change_keeps_same_dk():
    dk = keys.new_data_key()
    new_header = keys.wrap(keys.unwrap(keys.wrap(dk, PW), PW), "new password here")
    assert keys.unwrap(new_header, "new password here") == dk


def test_subkeys_are_deterministic_and_separated():
    dk = keys.new_data_key()
    f1 = bytes(16)
    f2 = bytes([1]) * 16
    assert keys.file_key(dk, f1) == keys.file_key(dk, f1)
    assert keys.file_key(dk, f1) != keys.file_key(dk, f2)
    assert keys.index_key(dk) != keys.file_key(dk, f1)
    assert len(keys.index_key(dk)) == 32
    assert keys.index_key(dk) != keys.index_key(keys.new_data_key())


def test_bad_header_json():
    with pytest.raises(keys.KeyHeaderError):
        keys.KeyHeader.from_json({"v": 2, "pw_salt": "", "wrapped_dk": ""})
    with pytest.raises(keys.KeyHeaderError):
        keys.KeyHeader.from_json({"v": 1})
