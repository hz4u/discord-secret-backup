import json

import httpx
import pytest

from secret.core import discord_api as d
from tests.fake_discord import FakeDiscord


def client_with(handler, **kw):
    sleeps = []
    c = d.HttpDiscord("TOKEN", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    return c, sleeps


def test_auth_header_and_json():
    def handler(req: httpx.Request):
        assert req.headers["Authorization"] == "Bot TOKEN"
        assert req.url.path == "/api/v10/users/@me"
        return httpx.Response(200, json={"id": "1", "username": "bot"})

    c, _ = client_with(handler)
    assert c.me()["username"] == "bot"


def test_send_file_multipart():
    seen = {}

    def handler(req: httpx.Request):
        seen["ctype"] = req.headers["content-type"]
        seen["body"] = req.content
        return httpx.Response(200, json={"id": "5", "attachments": []})

    c, _ = client_with(handler)
    c.send_file("10", "abc", "x.bin", b"\x00\x01DATA")
    assert seen["ctype"].startswith("multipart/form-data")
    assert b"payload_json" in seen["body"]
    assert b'"filename": "x.bin"' in seen["body"]
    assert b"\x00\x01DATA" in seen["body"]


def test_rate_limit_waits_then_succeeds():
    calls = {"n": 0}
    waits = []

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"retry_after": 2.5, "global": False})
        return httpx.Response(200, json={"id": "1", "username": "bot"})

    c, sleeps = client_with(handler, on_wait=waits.append)
    c.me()
    assert sleeps == [2.5] and waits == [2.5]


def test_server_error_retries_with_backoff():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(502) if calls["n"] <= 3 else httpx.Response(200, json={"id": "1", "username": "b"})

    c, sleeps = client_with(handler)
    c.me()
    assert sleeps == [1, 2, 4]


def test_gives_up_after_five_retries():
    c, sleeps = client_with(lambda req: httpx.Response(503))
    with pytest.raises(d.NetworkError):
        c.me()
    assert sleeps == [1, 2, 4, 8, 16]


def test_transport_error_retries():
    def handler(req):
        raise httpx.ConnectError("down")

    c, sleeps = client_with(handler)
    with pytest.raises(d.NetworkError):
        c.me()
    assert len(sleeps) == 5


@pytest.mark.parametrize("status, exc", [(401, d.DiscordAuthError), (403, d.DiscordPermissionError), (404, d.DiscordNotFound), (400, d.DiscordError)])
def test_status_mapping(status, exc):
    c, _ = client_with(lambda req: httpx.Response(status, json={"message": "x"}))
    with pytest.raises(exc):
        c.me()


def test_delete_returns_none_on_204():
    c, _ = client_with(lambda req: httpx.Response(204))
    assert c.delete_channel("1") is None


def test_compute_permissions():
    guild = {"id": "9", "owner_id": "100", "roles": [
        {"id": "9", "permissions": str(1 << 10)},
        {"id": "20", "permissions": str((1 << 11) | (1 << 15))},
        {"id": "30", "permissions": str(1 << 3)},
    ]}
    perms = d.compute_permissions(guild, {"roles": ["20"]}, "1")
    assert perms == (1 << 10) | (1 << 11) | (1 << 15)
    missing = d.missing_permissions(perms)
    assert "채널 관리" in missing and "채널 보기" not in missing and "파일 첨부" not in missing
    assert d.compute_permissions(guild, {"roles": ["30"]}, "1") == -1
    assert d.compute_permissions(guild, {"roles": []}, "100") == -1
    assert d.missing_permissions(-1) == []


def test_control_candidates():
    chans = [
        {"id": "1", "name": "secret", "type": 4, "parent_id": None},
        {"id": "2", "name": "index", "type": 0, "parent_id": "1"},
        {"id": "3", "name": "index", "type": 0, "parent_id": None},
        {"id": "4", "name": "secret", "type": 4, "parent_id": None},
    ]
    assert d.control_candidates(chans) == [("1", "2"), ("4", None)]
    assert d.control_candidates(chans[2:3]) == []


class _Index:
    def __init__(self, channels, messages):
        self._channels, self._messages = channels, messages

    def channels(self, guild_id):
        return self._channels

    def recent_messages(self, channel_id, limit=50):
        return self._messages.get(channel_id, [])[:limit]


def test_locate_control_prefers_the_remembered_then_the_bots_own_index():
    chans = [
        {"id": "c1", "name": "secret", "type": 4}, {"id": "i1", "name": "index", "type": 0, "parent_id": "c1"},
        {"id": "c2", "name": "secret", "type": 4}, {"id": "i2", "name": "index", "type": 0, "parent_id": "c2"},
    ]
    mine = {"id": "m", "content": "secret-index", "author": {"id": "bot"}}
    other = {"id": "o", "content": "secret-index", "author": {"id": "someone"}}
    client = _Index(chans, {"i1": [other], "i2": [mine]})
    assert d.locate_control(client, "g", "bot") == ("c2", "i2", [mine])
    empty = _Index(chans, {})
    assert d.locate_control(empty, "g", "bot", saved_index="i2")[:2] == ("c2", "i2")
    assert d.locate_control(empty, "g", "bot")[:2] == ("c1", "i1")
    assert d.locate_control(_Index([], {}), "g", "bot") == (None, None, [])


def test_check_connection_with_fake():
    fake = FakeDiscord()
    info = d.check_connection(fake, fake.guild_id)
    assert info.bot_name == "secret-bot" and info.guild_name == "테스트 서버"
    assert info.missing == [] and not info.has_backup
    cat = fake.create_category(fake.guild_id, "secret")
    idx = fake.create_text_channel(fake.guild_id, "index", cat)
    fake.send_file(idx, "secret-index v1", "index.bin", b"x")
    assert d.check_connection(fake, fake.guild_id).has_backup


def test_fetch_guild_icon():
    def handler(req):
        assert req.url.host == "cdn.discordapp.com"
        assert req.url.path == "/icons/9/abc.png"
        return httpx.Response(200, content=b"PNGDATA")

    c, _ = client_with(handler)
    assert d.fetch_guild_icon(c, "9", "abc") == b"PNGDATA"
    assert d.fetch_guild_icon(c, "9", None) is None
    broken, _ = client_with(lambda req: httpx.Response(404))
    assert d.fetch_guild_icon(broken, "9", "abc") is None


def test_check_connection_unknown_guild():
    with pytest.raises(d.DiscordNotFound):
        d.check_connection(FakeDiscord(), "nope")


def test_payload_json_content():
    seen = {}

    def handler(req):
        body = req.content.decode("latin-1")
        start = body.index("{")
        seen["payload"] = json.loads(body[start : body.index("}]}") + 3])
        return httpx.Response(200, json={"id": "1", "attachments": []})

    c, _ = client_with(handler)
    c.send_file("1", "hello", "a.bin", b"z")
    assert seen["payload"] == {"content": "hello", "attachments": [{"id": 0, "filename": "a.bin"}]}


def test_messages_after_pages_from_the_start():
    def handler(req: httpx.Request):
        assert req.url.path == "/api/v10/channels/77/messages"
        assert dict(req.url.params) == {"after": "123", "limit": "100"}
        return httpx.Response(200, json=[{"id": "124"}])

    c, _ = client_with(handler)
    assert c.messages_after("77", "123") == [{"id": "124"}]


def test_fake_messages_after_matches_discord_order():
    fake = FakeDiscord()
    cid = fake.create_text_channel(fake.guild_id, "c", fake.create_category(fake.guild_id, "v"))
    ids = [fake.send_message(cid, str(i))["id"] for i in range(5)]
    page = fake.messages_after(cid, ids[1], 2)
    assert [m["id"] for m in page] == [ids[3], ids[2]]


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.now += s

    def __call__(self):
        return self.now


def test_route_key_groups_by_channel():
    assert d.route_key("POST", "/channels/111/messages") == "POST /channels/111/messages"
    assert d.route_key("DELETE", "/channels/111/messages/999") == "DELETE /channels/111/messages/:id"
    assert d.route_key("POST", "/channels/111/messages") != d.route_key("POST", "/channels/222/messages")


def test_limiter_waits_before_the_bucket_runs_out():
    clock = _Clock()
    lim = d.RateLimiter(sleep=clock.sleep, clock=clock)
    key = "POST /channels/1/messages"
    assert lim.wait_turn(key) == 0
    lim.update(key, {"X-RateLimit-Bucket": "b", "X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "2.5"})
    assert lim.wait_turn(key) == 0
    wait = 2.5 + d.RESET_MARGIN
    assert lim.wait_turn(key) == pytest.approx(wait)
    assert clock.slept == [pytest.approx(wait)]


def test_limiter_429_makes_everyone_wait():
    clock = _Clock()
    lim = d.RateLimiter(sleep=clock.sleep, clock=clock)
    key = "POST /channels/1/messages"
    lim.update(key, {"X-RateLimit-Bucket": "b"}, limited_for=1.5)
    assert lim.wait_turn(key) == 1.5


def test_http_client_sends_one_channel_without_hitting_429(tmp_path):
    from secret.core import keys
    from secret.core.config_store import ConfigStore, Settings
    from secret.core.sync_engine import SyncEngine
    from tests.mock_server import MockDiscordServer

    server = MockDiscordServer(per_channel=(5, 0.4))
    root = tmp_path / "usb"
    (root / "사진/여행").mkdir(parents=True)
    for i in range(14):
        (root / "사진/여행" / f"{i:02d}.jpg").write_bytes(bytes([i]) * 50)
    store = ConfigStore.create(root / ".secret/config.dat", "pw", Settings(dk=keys.new_data_key(), token="t",
                                                                           guild_id=server.fake.guild_id))
    client = d.HttpDiscord("t", transport=server.transport())
    engine = SyncEngine(root, client, store, workers=4)
    engine.load_manifest()
    assert engine.run(engine.plan()).complete
    assert server.rate_limited <= 1
    assert server.wasted_bytes <= 200


def test_unexpected_error_does_not_block_the_route():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return httpx.Response(200, json={"id": "1", "username": "bot"})

    c, _ = client_with(handler)
    with pytest.raises(RuntimeError):
        c.me()
    assert c.me()["username"] == "bot"


def test_limiter_ignores_stale_out_of_order_responses():
    clock = _Clock()
    lim = d.RateLimiter(sleep=clock.sleep, clock=clock)
    key = "POST /channels/1/messages"
    h = {"X-RateLimit-Bucket": "b", "X-RateLimit-Reset-After": "5"}
    lim.update(key, {**h, "X-RateLimit-Remaining": "0"})
    lim.update(key, {**h, "X-RateLimit-Remaining": "3"})
    assert lim.wait_turn(key) > 0


def test_channels_do_not_share_one_limit_even_with_the_same_bucket_name():
    clock = _Clock()
    lim = d.RateLimiter(sleep=clock.sleep, clock=clock)
    a, b = "POST /channels/1/messages", "POST /channels/2/messages"
    same = {"X-RateLimit-Bucket": "hash", "X-RateLimit-Limit": "5", "X-RateLimit-Reset-After": "5"}
    lim.wait_turn(b)
    lim.update(b, {**same, "X-RateLimit-Remaining": "4"})
    lim.wait_turn(a)
    lim.update(a, {**same, "X-RateLimit-Remaining": "0"})
    assert lim.wait_turn(b) == 0
    assert lim.wait_turn(a) > 0


def test_uploads_to_several_channels_run_in_parallel(tmp_path):
    import time as _time

    from secret.core import keys
    from secret.core.config_store import ConfigStore, Settings
    from secret.core.sync_engine import SyncEngine
    from tests.mock_server import MockDiscordServer

    server = MockDiscordServer(per_channel=(2, 0.5))
    root = tmp_path / "usb"
    for folder in ("가/나", "다/라", "마/바", "사/아"):
        (root / folder).mkdir(parents=True)
        for i in range(4):
            (root / folder / f"{i}.jpg").write_bytes(bytes([i]) * 20)
    store = ConfigStore.create(root / ".secret/config.dat", "pw", Settings(dk=keys.new_data_key(), token="t",
                                                                           guild_id=server.fake.guild_id))
    engine = SyncEngine(root, d.HttpDiscord("t", transport=server.transport()), store, workers=4)
    engine.load_manifest()
    plan = engine.plan()
    start = _time.monotonic()
    assert engine.run(plan).complete
    assert _time.monotonic() - start < 2.5
    assert server.rate_limited == 0
