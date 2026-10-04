import json
import os
from pathlib import Path


def path_for(root: Path) -> Path:
    return root / ".secret" / "prefs.json"


def load(root: Path) -> dict:
    try:
        data = json.loads(path_for(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(root: Path, prefs: dict) -> None:
    p = path_for(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
