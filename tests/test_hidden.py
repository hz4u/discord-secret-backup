import os
import stat
import sys

import pytest

from secret.core.config_store import ConfigStore, Settings
from secret.core.hidden import ensure_hidden_dir, hide

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 숨김 속성")


def is_hidden(path) -> bool:
    return bool(os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)


def test_ensure_hidden_dir_creates_and_hides(tmp_path):
    d = ensure_hidden_dir(tmp_path / ".secret")
    assert d.is_dir() and is_hidden(d)
    (d / "x.txt").write_text("still writable")
    assert (d / "x.txt").read_text() == "still writable"


def test_hide_existing_visible_folder(tmp_path):
    d = tmp_path / ".secret"
    d.mkdir()
    assert not is_hidden(d)
    hide(d)
    assert is_hidden(d)
    hide(d)
    assert is_hidden(d)


def test_hide_missing_path_is_noop(tmp_path):
    hide(tmp_path / "없음")


def test_set_folder_hidden_roundtrip(tmp_path):
    from secret.core.hidden import is_hidden_or_system, set_folder_hidden

    d = tmp_path / "사진"
    d.mkdir()
    (d / "a.jpg").write_bytes(b"x")
    assert not is_hidden_or_system(d)
    set_folder_hidden(d, True)
    attrs = os.stat(d).st_file_attributes
    assert attrs & stat.FILE_ATTRIBUTE_HIDDEN and attrs & stat.FILE_ATTRIBUTE_SYSTEM
    assert is_hidden_or_system(d)
    assert not is_hidden(d / "a.jpg")
    set_folder_hidden(d, False)
    assert not is_hidden_or_system(d)


def test_config_store_hides_its_folder(tmp_path):
    store = ConfigStore.create(tmp_path / ".secret" / "config.dat", "pw pw pw pw pw", Settings(dk=bytes(32)))
    assert is_hidden(tmp_path / ".secret")
    store.save()
    assert ConfigStore.open(tmp_path / ".secret" / "config.dat", "pw pw pw pw pw").settings.dk == bytes(32)
