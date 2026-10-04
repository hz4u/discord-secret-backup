import pytest

from secret.core.config_store import MAGIC, ConfigCorruptError, ConfigStore, Settings, backup_path
from secret.core.crypto_core import DecryptionError

PW = "central passphrase for secret"


@pytest.fixture
def path(tmp_path):
    return tmp_path / ".secret" / "config.dat"


def make_settings(**kw):
    return Settings(dk=bytes(range(32)), **kw)


def test_create_and_open(path):
    ConfigStore.create(path, PW, make_settings(token="tok", guild_id="123", guild_name="내 서버"))
    assert path.read_bytes().startswith(MAGIC)
    store = ConfigStore.open(path, PW)
    s = store.settings
    assert (s.token, s.guild_id, s.guild_name, s.dk) == ("tok", "123", "내 서버", bytes(range(32)))
    assert s.journal == {} and s.history == [] and s.manifest is None
    assert store.password == PW


def test_not_connected_by_default(path):
    store = ConfigStore.create(path, PW, make_settings())
    assert not store.settings.connected
    store.settings.token, store.settings.guild_id = "t", "1"
    assert store.settings.connected


def test_plaintext_not_in_file(path):
    ConfigStore.create(path, PW, make_settings(token="SUPER-SECRET-TOKEN"))
    assert b"SUPER-SECRET-TOKEN" not in path.read_bytes()


def test_wrong_password(path):
    ConfigStore.create(path, PW, make_settings())
    with pytest.raises(DecryptionError):
        ConfigStore.open(path, "wrong")


def test_save_roundtrip_and_backup(path):
    store = ConfigStore.create(path, PW, make_settings())
    store.settings.last_sync = "2026-09-28T10:00:00"
    store.settings.journal = {"x": 1}
    store.settings.manifest = {"files": {}}
    store.save()
    assert backup_path(path).exists()
    again = ConfigStore.open(path, PW)
    assert again.settings.last_sync == "2026-09-28T10:00:00"
    assert again.settings.journal == {"x": 1}
    assert again.settings.manifest == {"files": {}}
    old = ConfigStore.open(path, PW, source=backup_path(path))
    assert old.settings.last_sync is None


@pytest.mark.parametrize("corrupt", [lambda b: b"XXXXXXX" + b[7:], lambda b: b[:20], lambda b: b""])
def test_corrupt(path, corrupt):
    ConfigStore.create(path, PW, make_settings())
    path.write_bytes(corrupt(path.read_bytes()))
    with pytest.raises(ConfigCorruptError):
        ConfigStore.open(path, PW)


def test_change_password(path):
    store = ConfigStore.create(path, PW, make_settings(token="t"))
    store.change_password("brand new passphrase")
    assert store.password == "brand new passphrase"
    with pytest.raises(DecryptionError):
        ConfigStore.open(path, PW)
    assert ConfigStore.open(path, "brand new passphrase").settings.token == "t"


def test_exists(path):
    assert not ConfigStore.exists(path)
    ConfigStore.create(path, PW, make_settings())
    assert ConfigStore.exists(path)


def test_unknown_settings_from_a_newer_version_are_ignored():
    data = Settings(dk=b"k" * 32, token="t").to_json()
    data["something_new"] = [1, 2, 3]
    s = Settings.from_json(data)
    assert s.token == "t" and s.reupload == []
