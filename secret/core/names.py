import re

from ..i18n import tr

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def validate_name(name: str) -> str | None:
    if not name.strip():
        return tr("이름이 비어 있습니다.")
    if name in (".", ".."):
        return tr("쓸 수 없는 이름입니다.")
    if _INVALID.search(name):
        return tr("이름에 쓸 수 없는 문자가 있습니다: < > : \" / \\ | ? *")
    if name[-1] in ". ":
        return tr("이름 끝에 점이나 공백을 쓸 수 없습니다.")
    if name.split(".")[0].upper() in _RESERVED:
        return tr("'{name}'은(는) 윈도우가 예약된 이름으로 씁니다.", name=name)
    if len(name) > 255:
        return tr("이름이 너무 깁니다.")
    return None
