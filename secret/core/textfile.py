import codecs
import os
from dataclasses import dataclass, replace
from pathlib import Path

MAX_EDIT_BYTES = 5 * 1024 * 1024
EDITABLE_EXTS = {
    "txt", "md", "csv", "tsv", "log", "json", "xml", "html", "htm", "ini", "cfg", "conf", "yaml", "yml", "toml",
    "srt", "smi", "vtt",
    "css", "js", "py", "bat", "cmd", "ps1", "sh", "sql",
}

_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


class NotText(Exception):
    pass


class TooLarge(Exception):
    pass


class ChangedOnDisk(Exception):
    pass


@dataclass(frozen=True)
class TextDoc:
    text: str
    encoding: str
    bom: bool
    newline: str
    size: int
    mtime_ns: int
    switched_to_utf8: bool = False


def _newline_of(text: str) -> str:
    i = text.find("\r")
    j = text.find("\n")
    if i == -1 and j == -1:
        return "\r\n"
    if i != -1 and (j == -1 or i < j):
        return "\r\n" if text[i : i + 2] == "\r\n" else "\r"
    return "\n"


def _decode(data: bytes) -> tuple[str, str, bool]:
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode("utf-8-sig" if encoding == "utf-8" else "utf-16"), encoding, True
            except UnicodeDecodeError as exc:
                raise NotText() from exc
    if b"\x00" in data:
        raise NotText()
    for encoding in ("utf-8", "cp949"):
        try:
            return data.decode(encoding), encoding, False
        except UnicodeDecodeError:
            continue
    raise NotText()


def decode_text(data: bytes) -> str:
    if len(data) > MAX_EDIT_BYTES:
        raise TooLarge()
    text, _, _ = _decode(data)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load(path: Path) -> TextDoc:
    st = path.stat()
    if st.st_size > MAX_EDIT_BYTES:
        raise TooLarge()
    data = path.read_bytes()
    text, encoding, bom = _decode(data)
    newline = _newline_of(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return TextDoc(text, encoding, bom, newline, st.st_size, st.st_mtime_ns)


def _encode(text: str, encoding: str, bom: bool) -> bytes:
    if encoding == "utf-16":
        return text.encode("utf-16")
    body = text.encode(encoding)
    return codecs.BOM_UTF8 + body if bom and encoding == "utf-8" else body


def save(path: Path, doc: TextDoc, text: str, force: bool = False) -> TextDoc:
    try:
        st = path.stat()
        if not force and (st.st_size, st.st_mtime_ns) != (doc.size, doc.mtime_ns):
            raise ChangedOnDisk()
    except FileNotFoundError:
        pass

    raw = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", doc.newline)
    encoding, switched = doc.encoding, False
    try:
        data = _encode(raw, encoding, doc.bom)
    except UnicodeEncodeError:
        encoding, switched = "utf-8", True
        data = _encode(raw, encoding, False)

    tmp = path.with_name(f".{path.name}.secret-tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    st = path.stat()
    return replace(doc, text=text, encoding=encoding, bom=doc.bom and not switched,
                   size=st.st_size, mtime_ns=st.st_mtime_ns, switched_to_utf8=switched)
