import json
import string
import sys
from pathlib import Path

LANGUAGES = {
    "en": "English",
    "ko": "한국어",
    "zh": "简体中文",
    "ja": "日本語",
    "ru": "Русский",
}
SOURCE = "ko"
FALLBACK = "en"

_current = SOURCE
_tables: dict[str, dict[str, str | dict[str, str]]] = {}


def _lang_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parents[1]
    return Path(base) / "assets" / "lang"


def _table(lang: str) -> dict:
    if lang == SOURCE:
        return {}
    if lang not in _tables:
        try:
            _tables[lang] = json.loads((_lang_dir() / f"{lang}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _tables[lang] = {}
    return _tables[lang]


def set_language(lang: str) -> None:
    global _current
    _current = lang if lang in LANGUAGES else FALLBACK


def current_language() -> str:
    return _current


def plural_form(lang: str, n: int) -> str:
    if lang == "en":
        return "one" if n == 1 else "other"
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        return "few" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "many"
    return "other"


def _count(text: str, values: dict) -> int:
    for _, name, _, _ in string.Formatter().parse(text):
        if isinstance(values.get(name), int):
            return values[name]
    return 0


def tr(text: str, /, **values) -> str:
    lang = _current if text in _table(_current) else FALLBACK
    out = _table(_current).get(text) or (_table(FALLBACK).get(text) if _current != SOURCE else None) or text
    if isinstance(out, dict):
        out = out.get(plural_form(lang, _count(text, values))) or out["other"]
    if out is not text:
        lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
        out = lead + out.strip() + trail
    return out.format(**values) if values else out
