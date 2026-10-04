import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bundled_sources() -> list[str]:
    bat = (ROOT / "build.bat").read_text(encoding="utf-8")
    return [src.replace("\\", "/") for src in re.findall(r'--add-data "([^;"]+);[^"]*"', bat)]


def test_every_resource_the_code_opens_is_bundled():
    used = set()
    for py in (ROOT / "secret").rglob("*.py"):
        used |= set(re.findall(r'resource\("([^"]+)"\)', py.read_text(encoding="utf-8")))
    assert used, "resource() 호출을 찾지 못함"
    sources = bundled_sources()
    missing = [u for u in used if not any(u == s or u.startswith(s + "/") for s in sources)]
    assert missing == []


def test_window_icon_is_bundled():
    assert "assets/secret.ico" in bundled_sources()


def test_icons_folder_is_bundled():
    assert "assets/icons" in bundled_sources()


def test_translations_are_bundled():
    assert "assets/lang" in bundled_sources()


def test_modules_the_code_needs_are_not_excluded():
    bat = (ROOT / "build.bat").read_text(encoding="utf-8")
    excluded = set(re.findall(r"--exclude-module (\S+)", bat))
    assert not excluded & {"PIL", "pillow_heif", "PySide6.QtPdf"}
