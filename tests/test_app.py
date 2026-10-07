from pathlib import Path

from secret.app import frozen_root, resolve_root, startup_language
from secret.core import prefs


def test_usb_uses_drive_root():
    assert frozen_root(Path(r"E:\Secret.exe"), "C:") == Path("E:\\")
    assert frozen_root(Path(r"E:\도구\Secret.exe"), "C:") == Path("E:\\")


def test_system_drive_uses_exe_folder():
    assert frozen_root(Path(r"C:\Users\me\Desktop\Secret.exe"), "C:") == Path(r"C:\Users\me\Desktop")
    assert frozen_root(Path(r"c:\x\Secret.exe"), "C:") == Path(r"c:\x")


def test_root_argument_and_env(tmp_path, monkeypatch):
    assert resolve_root(["--root", str(tmp_path)]) == tmp_path.resolve()
    monkeypatch.setenv("SECRET_ROOT", str(tmp_path))
    assert resolve_root([]) == tmp_path.resolve()


def test_english_until_a_language_is_chosen(tmp_path):
    assert startup_language(tmp_path) == "en"
    prefs.save(tmp_path, {"language": "ko"})
    assert startup_language(tmp_path) == "ko"
