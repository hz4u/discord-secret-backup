import ast
import json
import re
import string
from pathlib import Path

import pytest

from secret import i18n

ROOT = Path(__file__).resolve().parents[1]
LANG_DIR = ROOT / "assets" / "lang"


def catalog() -> dict[str, str]:
    found = {}
    for p in sorted((ROOT / "secret").rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                found.setdefault(node.args[0].value, f"{p.relative_to(ROOT).as_posix()}:{node.lineno}")
    return found


def fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f is not None}


@pytest.fixture
def lang(monkeypatch):
    def use(code, table=None, fallback=None):
        monkeypatch.setattr(i18n, "_tables", {"en": fallback or {}, code: table or {}})
        i18n.set_language(code)

    yield use
    i18n.set_language("ko")


def test_korean_is_the_source(lang):
    lang("ko")
    assert i18n.tr("파일 {n}개", n=3) == "파일 3개"


def test_translation_then_english_then_korean(lang):
    lang("ja", {"동기화": "同期"}, fallback={"동기화": "Sync", "휴지통": "Trash"})
    assert i18n.tr("동기화") == "同期"
    assert i18n.tr("휴지통") == "Trash"
    assert i18n.tr("처음 보는 문구") == "처음 보는 문구"


def test_edge_spaces_and_line_breaks_are_kept(lang):
    lang("en", {"   휴지통": "Trash", "\n\n지울 파일은 남습니다.": "  Files stay.  "})
    assert i18n.tr("   휴지통") == "   Trash"
    assert i18n.tr("\n\n지울 파일은 남습니다.") == "\n\nFiles stay."


def test_plural_forms_follow_the_first_count(lang):
    lang("en", {"파일 {n}개 ({size})": {"one": "{n} file ({size})", "other": "{n} files ({size})"}})
    assert i18n.tr("파일 {n}개 ({size})", n=1, size="2 B") == "1 file (2 B)"
    assert i18n.tr("파일 {n}개 ({size})", n=21, size="2 B") == "21 files (2 B)"


def test_russian_plural_rules():
    assert [i18n.plural_form("ru", n) for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)] == \
        ["one", "few", "many", "many", "many", "one", "few", "many", "many"]


def test_unknown_language_falls_back_to_english():
    i18n.set_language("xx")
    assert i18n.current_language() == "en"
    i18n.set_language("ko")


def test_every_ui_string_goes_through_tr():
    hangul = re.compile(r"[가-힣]")
    left = []
    for p in (ROOT / "secret").rglob("*.py"):
        if p.name == "i18n.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs, parent = set(), {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parent[id(c)] = n
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and isinstance(n.body[0], ast.Expr):
                docs.add(id(n.body[0].value))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and hangul.search(n.value) and id(n) not in docs:
                q = parent.get(id(n))
                while q is not None and not (isinstance(q, ast.Call) and getattr(q.func, "id", "") == "tr"):
                    q = parent.get(id(q))
                if q is None:
                    left.append(f"{p.relative_to(ROOT)}:{n.lineno} {n.value[:30]!r}")
    assert left == []


@pytest.mark.parametrize("code", [c for c in i18n.LANGUAGES if c != i18n.SOURCE])
def test_translation_files_match_the_code(code):
    path = LANG_DIR / f"{code}.json"
    if not path.exists():
        pytest.skip(f"{code}.json 아직 없음")
    table = json.loads(path.read_text(encoding="utf-8"))
    source = catalog()
    missing = sorted(set(source) - set(table))
    assert missing == [], f"{code}: 번역 없음 {len(missing)}개, 예: {missing[:5]}"
    stale = sorted(set(table) - set(source))
    assert stale == [], f"{code}: 코드에 없는 원문 {stale[:5]}"
    forms = {"en": {"one", "other"}, "ru": {"one", "few", "many", "other"}}.get(code, {"other"})
    wrong = []
    for k, v in table.items():
        variants = v if isinstance(v, dict) else {"other": v}
        if "other" not in variants or set(variants) - forms:
            wrong.append(k)
        elif any(fields(k) != fields(t) for t in variants.values()):
            wrong.append(k)
    assert wrong == [], f"{code}: 자리 표시나 복수형이 다름 {wrong[:5]}"
