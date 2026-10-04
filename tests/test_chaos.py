import os
import random
import threading

import pytest

from secret.core import history, keys
from secret.core.config_store import ConfigStore, Settings
from secret.core.discord_api import NetworkError
from secret.core.restore import open_backup, restore
from secret.core.sync_engine import SyncEngine
from tests.fake_discord import FakeDiscord

PW = "central passphrase for secret"
SEEDS = int(os.environ.get("CHAOS_SEEDS", "12"))
PART = 1000
WRITES = {"send_message", "send_file", "create_thread", "delete_message", "delete_channel", "create_category", "create_text_channel"}


class Crash(BaseException):
    pass


class Faulty:
    def __init__(self, inner: FakeDiscord, fail_at: int, kind: str):
        self.inner, self.fail_at, self.kind = inner, fail_at, kind
        self.writes = 0
        self.broken = False
        self._lock = threading.Lock()

    def _fail(self):
        raise Crash() if self.kind == "crash" else NetworkError("인터넷이 끊김")

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            with self._lock:
                if name in WRITES:
                    self.writes += 1
                    if self.writes >= self.fail_at:
                        self.broken = True
                broken = self.broken
            if broken:
                self._fail()
            return attr(*args, **kwargs)

        return call


def _write(root, rel: str, data: bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _files(root) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file() and ".secret" not in p.relative_to(root).parts}


def _make_vault(rng: random.Random, root) -> None:
    for i in range(rng.randint(6, 10)):
        folder = rng.choice(["사진/여행", "사진/일상", "문서", "영상/게임", ""])
        _write(root, f"{folder}/f{i}.jpg".lstrip("/"), rng.randbytes(rng.randint(1, 3500)))


def _mutate(rng: random.Random, root) -> None:
    files = sorted(_files(root))
    for rel in rng.sample(files, k=min(3, len(files))):
        op = rng.choice(["edit", "delete", "keep"])
        if op == "edit":
            _write(root, rel, rng.randbytes(rng.randint(1, 3500)))
        elif op == "delete":
            (root / rel).unlink()
    for i in range(rng.randint(1, 4)):
        _write(root, f"{rng.choice(['사진/여행', '새폴더', '문서'])}/new{i}.jpg", rng.randbytes(rng.randint(1, 3500)))
    if rng.random() < 0.4 and (root / "문서").exists():
        (root / "문서").rename(root / "서류")


def _sync(root, client, cfg):
    store = ConfigStore.open(cfg, PW)
    engine = SyncEngine(root, client, store, part_size=PART, keep_versions=5, keep_trash=20)
    return engine.run(engine.plan())


@pytest.mark.parametrize("kind", ["crash", "network"])
@pytest.mark.parametrize("seed", range(SEEDS))
def test_sync_survives_a_failure_at_any_point(tmp_path, seed, kind):
    rng = random.Random(seed * 7919 + (kind == "crash"))
    root = tmp_path / "usb"
    root.mkdir()
    fake = FakeDiscord()
    cfg = root / ".secret" / "config.dat"
    ConfigStore.create(cfg, PW, Settings(dk=keys.new_data_key(), token="t", guild_id=fake.guild_id))
    _make_vault(rng, root)
    assert _sync(root, fake, cfg).complete

    _mutate(rng, root)
    faulty = Faulty(fake, fail_at=rng.randint(1, 30), kind=kind)
    try:
        _sync(root, faulty, cfg)
    except (Crash, NetworkError):
        pass

    result = _sync(root, fake, cfg)
    assert result.complete, (seed, kind, faulty.writes, result)

    target = tmp_path / "restored"
    target.mkdir()
    r = restore(fake, fake.guild_id, PW, target)
    assert not r.failed and not r.mismatched
    assert _files(target) == _files(root), (seed, kind, faulty.writes)

    m, *_ = open_backup(fake, fake.guild_id, PW)
    alive = {msg["id"] for msg in fake.all_messages()}
    for e in history.references(m.files, m.history, m.trash).values():
        assert {e.message_id, *e.part_ids} <= alive, (seed, kind, e)
