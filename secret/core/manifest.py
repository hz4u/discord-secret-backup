import json
import struct
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import chunks, keys
from .crypto_core import TAG_LEN, CryptoError

INDEX_MAGIC = b"SECIDX1"
INNER_CHUNK = 1024 * 1024
_VERSION = 1


class ManifestError(CryptoError):
    pass


@dataclass
class FileEntry:
    size: int
    mtime_ns: int
    sha256: str
    file_id: str
    nonce_prefix: str
    n: int
    channel_id: str
    message_id: str
    thread_id: str | None
    part_ids: list[str]


def _entry(d: dict | None) -> "FileEntry | None":
    return FileEntry(**d) if d is not None else None


def _plain(e: "FileEntry | None") -> dict | None:
    return asdict(e) if e is not None else None


@dataclass
class Version:
    id: int
    at: str
    kind: str
    summary: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)
    dirs: list = field(default_factory=list)
    channels: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)
    digest: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id, "at": self.at, "kind": self.kind, "summary": self.summary, "digest": self.digest,
            "changes": {rel: [_plain(b), _plain(a)] for rel, (b, a) in self.changes.items()},
            "dirs": self.dirs, "channels": self.channels, "categories": self.categories,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Version":
        return cls(
            id=int(d["id"]), at=d["at"], kind=d["kind"], summary=dict(d.get("summary", {})),
            changes={rel: (_entry(b), _entry(a)) for rel, (b, a) in d["changes"].items()},
            dirs=list(d.get("dirs", [])), channels=dict(d.get("channels", {})), categories=dict(d.get("categories", {})),
            digest=d.get("digest", ""),
        )


@dataclass
class TrashItem:
    rel: str
    entry: FileEntry
    at: str
    batch: int

    def to_json(self) -> dict:
        return {"rel": self.rel, "entry": asdict(self.entry), "at": self.at, "batch": self.batch}

    @classmethod
    def from_json(cls, d: dict) -> "TrashItem":
        return cls(rel=d["rel"], entry=FileEntry(**d["entry"]), at=d["at"], batch=int(d["batch"]))


@dataclass
class Manifest:
    categories: dict[str, str] = field(default_factory=dict)
    channels: dict[str, str] = field(default_factory=dict)
    files: dict[str, FileEntry] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)
    updated: str = ""
    history: list[Version] = field(default_factory=list)
    trash: list[TrashItem] = field(default_factory=list)
    retired_channels: dict[str, str] = field(default_factory=dict)
    retired_categories: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "Manifest":
        return cls()

    def to_json(self) -> dict:
        return {
            "v": _VERSION,
            "updated": self.updated,
            "categories": self.categories,
            "channels": self.channels,
            "files": {rel: asdict(e) for rel, e in self.files.items()},
            "dirs": self.dirs,
            "history": [v.to_json() for v in self.history],
            "trash": [t.to_json() for t in self.trash],
            "retired_channels": self.retired_channels,
            "retired_categories": self.retired_categories,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Manifest":
        try:
            if data["v"] != _VERSION:
                raise ManifestError(f"unsupported manifest version {data['v']}")
            return cls(
                categories=dict(data["categories"]),
                channels=dict(data["channels"]),
                files={rel: FileEntry(**e) for rel, e in data["files"].items()},
                dirs=list(data["dirs"]),
                updated=data.get("updated", ""),
                history=[Version.from_json(v) for v in data.get("history", [])],
                trash=[TrashItem.from_json(t) for t in data.get("trash", [])],
                retired_channels=dict(data.get("retired_channels", {})),
                retired_categories=list(data.get("retired_categories", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError("bad manifest") from exc

    def touch(self) -> None:
        self.updated = datetime.now().isoformat(timespec="seconds")


def encode_index(manifest: Manifest, dk: bytes, header: keys.KeyHeader) -> bytes:
    plain = zlib.compress(json.dumps(manifest.to_json(), ensure_ascii=False).encode("utf-8"), 9)
    header_json = json.dumps(header.to_json()).encode("ascii")
    index_id, prefix = chunks.new_file_id(), chunks.new_nonce_prefix()
    n = chunks.part_count(len(plain), INNER_CHUNK)
    key = keys.index_key(dk)
    body = b"".join(
        chunks.encrypt_part(key, index_id, prefix, i, n, plain[i * INNER_CHUNK : (i + 1) * INNER_CHUNK])
        for i in range(n)
    )
    return (
        INDEX_MAGIC
        + struct.pack(">I", len(header_json))
        + header_json
        + index_id
        + prefix
        + struct.pack(">I", n)
        + body
    )


def _split(blob: bytes) -> tuple[keys.KeyHeader, bytes, bytes, int, bytes]:
    if not blob.startswith(INDEX_MAGIC) or len(blob) < len(INDEX_MAGIC) + 4:
        raise ManifestError("bad magic")
    pos = len(INDEX_MAGIC)
    (hlen,) = struct.unpack(">I", blob[pos : pos + 4])
    pos += 4
    try:
        header = keys.KeyHeader.from_json(json.loads(blob[pos : pos + hlen]))
    except (ValueError, keys.KeyHeaderError) as exc:
        raise ManifestError("bad key header") from exc
    pos += hlen
    fixed = chunks.FILE_ID_LEN + chunks.NONCE_PREFIX_LEN + 4
    if len(blob) < pos + fixed:
        raise ManifestError("truncated")
    index_id = blob[pos : pos + chunks.FILE_ID_LEN]
    pos += chunks.FILE_ID_LEN
    prefix = blob[pos : pos + chunks.NONCE_PREFIX_LEN]
    pos += chunks.NONCE_PREFIX_LEN
    (n,) = struct.unpack(">I", blob[pos : pos + 4])
    return header, index_id, prefix, n, blob[pos + 4 :]


def read_index_header(blob: bytes) -> keys.KeyHeader:
    return _split(blob)[0]


def decode_index(blob: bytes, dk: bytes) -> Manifest:
    _, index_id, prefix, n, body = _split(blob)
    step = INNER_CHUNK + TAG_LEN
    pieces = [body[i : i + step] for i in range(0, len(body), step)] or [b""]
    if len(pieces) != n:
        raise ManifestError("chunk count mismatch")
    key = keys.index_key(dk)
    plain = b"".join(chunks.decrypt_part(key, index_id, prefix, i, n, p) for i, p in enumerate(pieces))
    try:
        return Manifest.from_json(json.loads(zlib.decompress(plain).decode("utf-8")))
    except (zlib.error, ValueError) as exc:
        raise ManifestError("bad content") from exc
