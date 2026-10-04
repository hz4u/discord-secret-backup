import base64
import json
import os
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .crypto_core import NONCE_LEN, SALT_LEN, TAG_LEN, CryptoError, derive_key, seal, unseal
from .hidden import ensure_hidden_dir

MAGIC = b"SECCFG1"


class ConfigCorruptError(CryptoError):
    pass


@dataclass
class Settings:
    dk: bytes
    token: str = ""
    guild_id: str = ""
    guild_name: str = ""
    bot_name: str = ""
    guild_icon: str = ""
    guild_icon_hash: str = ""
    index_message_id: str | None = None
    index_channel_id: str | None = None
    manifest: dict | None = None
    journal: dict = field(default_factory=dict)
    last_sync: str | None = None
    history: list = field(default_factory=list)
    hidden_folders: list = field(default_factory=list)
    keep_versions: int | None = None
    keep_trash: int | None = None
    reupload: list = field(default_factory=list)

    @property
    def connected(self) -> bool:
        return bool(self.token and self.guild_id)

    def to_json(self) -> dict:
        data = asdict(self)
        data["dk"] = base64.b64encode(self.dk).decode()
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        data["dk"] = base64.b64decode(data["dk"])
        return cls(**data)


def backup_path(path: Path) -> Path:
    return path.with_suffix(".bak")


class ConfigStore:
    def __init__(self, path: Path, password: str, salt: bytes, kek: bytes, settings: Settings):
        self.path = path
        self.password = password
        self.settings = settings
        self._salt = salt
        self._kek = kek

    @staticmethod
    def exists(path: Path) -> bool:
        return path.exists() or backup_path(path).exists()

    @classmethod
    def create(cls, path: Path, password: str, settings: Settings) -> "ConfigStore":
        salt = os.urandom(SALT_LEN)
        store = cls(path, password, salt, derive_key(password, salt), settings)
        store.save()
        return store

    @classmethod
    def open(cls, path: Path, password: str, source: Path | None = None) -> "ConfigStore":
        data = (source or path).read_bytes()
        header = len(MAGIC) + SALT_LEN
        if not data.startswith(MAGIC) or len(data) < header + NONCE_LEN + TAG_LEN:
            raise ConfigCorruptError("bad header")
        salt = data[len(MAGIC) : header]
        kek = derive_key(password, salt)
        plaintext = unseal(kek, data[header:], MAGIC)
        try:
            settings = Settings.from_json(json.loads(plaintext.decode("utf-8")))
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigCorruptError("bad content") from exc
        return cls(path, password, salt, kek, settings)

    def save(self) -> None:
        payload = json.dumps(self.settings.to_json(), ensure_ascii=False).encode("utf-8")
        data = MAGIC + self._salt + seal(self._kek, payload, MAGIC)
        ensure_hidden_dir(self.path.parent)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if self.path.exists():
            shutil.copyfile(self.path, backup_path(self.path))
        os.replace(tmp, self.path)

    def change_password(self, new_password: str) -> None:
        self._salt = os.urandom(SALT_LEN)
        self._kek = derive_key(new_password, self._salt)
        self.password = new_password
        self.save()
