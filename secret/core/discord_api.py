import json
import threading
import time
from dataclasses import dataclass, field

import httpx

from ..i18n import tr

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/local/secret, 1.0)"
CONTROL_CATEGORY = "secret"
INDEX_CHANNEL = "index"
INDEX_PREFIX = "secret-index"

TYPE_TEXT = 0
TYPE_CATEGORY = 4
ADMINISTRATOR = 1 << 3
REQUIRED_PERMISSIONS: dict[str, int] = {
    tr("채널 보기"): 1 << 10,
    tr("채널 관리"): 1 << 4,
    tr("메시지 보내기"): 1 << 11,
    tr("파일 첨부"): 1 << 15,
    tr("메시지 기록 보기"): 1 << 16,
    tr("메시지 관리"): 1 << 13,
    tr("공개 스레드 만들기"): 1 << 35,
    tr("스레드에서 메시지 보내기"): 1 << 38,
    tr("스레드 관리"): 1 << 34,
}
_RETRY_DELAYS = (1, 2, 4, 8, 16)
_MAX_RATE_LIMIT_WAITS = 100


class DiscordError(Exception):
    pass


class DiscordAuthError(DiscordError):
    pass


class DiscordPermissionError(DiscordError):
    pass


class DiscordNotFound(DiscordError):
    pass


class NetworkError(DiscordError):
    pass


def compute_permissions(guild: dict, member: dict, user_id: str) -> int:
    if guild.get("owner_id") == user_id:
        return -1
    roles = {r["id"]: int(r["permissions"]) for r in guild.get("roles", [])}
    perms = roles.get(guild["id"], 0)
    for role_id in member.get("roles", []):
        perms |= roles.get(role_id, 0)
    if perms & ADMINISTRATOR:
        return -1
    return perms


def missing_permissions(perms: int) -> list[str]:
    if perms == -1:
        return []
    return [name for name, bit in REQUIRED_PERMISSIONS.items() if not perms & bit]


def guild_icon_url(guild_id: str, icon_hash: str, size: int = 128) -> str:
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size={size}"


def fetch_guild_icon(client, guild_id: str, icon_hash: str | None) -> bytes | None:
    if not icon_hash:
        return None
    try:
        return client.download(guild_icon_url(guild_id, icon_hash))
    except DiscordError:
        return None


def control_candidates(channels: list[dict]) -> list[tuple[str, str | None]]:
    out = []
    for cat in (c for c in channels if c["type"] == TYPE_CATEGORY and c["name"] == CONTROL_CATEGORY):
        index = next((c["id"] for c in channels
                      if c["type"] == TYPE_TEXT and c["name"] == INDEX_CHANNEL and c.get("parent_id") == cat["id"]), None)
        out.append((cat["id"], index))
    return out


def bot_index_messages(client, index: str, bot_id: str, limit: int = 20) -> list[dict]:
    return [m for m in client.recent_messages(index, limit)
            if m.get("author", {}).get("id") == bot_id and m.get("content", "").startswith(INDEX_PREFIX)]


def locate_control(client, guild_id: str, bot_id: str, saved_index: str | None = None) -> tuple[str | None, str | None, list[dict]]:
    candidates = control_candidates(client.channels(guild_id))
    with_index = sorted((p for p in candidates if p[1]), key=lambda p: p[1] != saved_index)
    for category, index in with_index:
        msgs = bot_index_messages(client, index, bot_id)
        if msgs:
            return category, index, msgs
    if with_index:
        return with_index[0][0], with_index[0][1], []
    if candidates:
        return candidates[0][0], None, []
    return None, None, []


RESET_MARGIN = 0.05


def route_key(method: str, path: str) -> str:
    parts = path.split("?")[0].strip("/").split("/")
    out = []
    for i, part in enumerate(parts):
        major = i > 0 and parts[i - 1] in ("channels", "guilds", "webhooks")
        out.append(part if major or not part.isdigit() else ":id")
    return f"{method} /" + "/".join(out)


def major_of(key: str) -> str:
    parts = key.split(" ", 1)[-1].strip("/").split("/")
    return "/".join(f"{p}/{parts[i + 1]}" for i, p in enumerate(parts[:-1]) if p in ("channels", "guilds", "webhooks"))


class RateLimiter:
    POLL = 0.02
    REPORT_AT = 0.5

    def __init__(self, sleep=time.sleep, clock=time.monotonic, on_wait=None):
        self._sleep = sleep
        self._clock = clock
        self.on_wait = on_wait
        self._lock = threading.Lock()
        self._bucket_of: dict[str, str] = {}
        self._state: dict[str, dict] = {}
        self._in_flight: dict[str, int] = {}

    def _name(self, key: str) -> str:
        return self._bucket_of.get(key, key)

    def wait_turn(self, key: str) -> float:
        waited = 0.0
        while True:
            deadline = None
            with self._lock:
                name = self._name(key)
                st = self._state.get(name)
                now = self._clock()
                flying = self._in_flight.get(name, 0)
                if st is not None and now >= st["reset_at"]:
                    st["remaining"], st["reset_at"] = st["limit"], float("inf")
                if st is None:
                    if flying == 0:
                        self._in_flight[name] = 1
                        return waited
                    pause = self.POLL
                elif st["remaining"] - flying > 0:
                    self._in_flight[name] = flying + 1
                    return waited
                elif st["reset_at"] == float("inf"):
                    pause = self.POLL
                else:
                    pause = st["reset_at"] - now
                    deadline = st["reset_at"]
            if pause >= self.REPORT_AT and self.on_wait:
                self.on_wait(pause)
            self._sleep(pause)
            waited += pause
            if deadline is not None:
                with self._lock:
                    if st["reset_at"] == deadline:
                        st["remaining"], st["reset_at"] = st["limit"], float("inf")

    def release(self, key: str) -> None:
        with self._lock:
            name = self._name(key)
            self._in_flight[name] = max(0, self._in_flight.get(name, 0) - 1)

    def update(self, key: str, headers, limited_for: float | None = None) -> None:
        bucket = headers.get("X-RateLimit-Bucket")
        with self._lock:
            old_name = self._name(key)
            flying = max(0, self._in_flight.pop(old_name, 0) - 1)
            if bucket:
                self._bucket_of[key] = f"{bucket}:{major_of(key)}"
            name = self._name(key)
            self._in_flight[name] = self._in_flight.get(name, 0) + flying
            now = self._clock()
            st = self._state.get(name)
            limit = headers.get("X-RateLimit-Limit")
            if limited_for is not None:
                if st is None:
                    st = self._state[name] = {"limit": 1, "remaining": 0, "reset_at": 0.0}
                st["remaining"], st["reset_at"] = 0, now + limited_for
                if limit:
                    st["limit"] = int(limit)
                return
            remaining = headers.get("X-RateLimit-Remaining")
            reset_after = headers.get("X-RateLimit-Reset-After")
            if remaining is None or reset_after is None:
                if st is None:
                    self._state[name] = {"limit": 1_000_000, "remaining": 1_000_000, "reset_at": float("inf")}
                return
            reset_at = now + float(reset_after) + RESET_MARGIN
            if st is not None and now < st["reset_at"] < float("inf"):
                st["remaining"] = min(st["remaining"], int(remaining))
            else:
                self._state[name] = st = {"limit": int(limit) if limit else int(remaining) + 1,
                                          "remaining": int(remaining), "reset_at": reset_at}
            if limit:
                st["limit"] = int(limit)


class HttpDiscord:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, sleep=time.sleep, on_wait=None, timeout: float = 120):
        headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
        self._api = httpx.Client(base_url=API, headers=headers, transport=transport, timeout=timeout)
        self._cdn = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True)
        self._sleep = sleep
        self.limiter = RateLimiter(sleep=sleep)
        self.on_wait = on_wait

    @property
    def on_wait(self):
        return self.limiter.on_wait

    @on_wait.setter
    def on_wait(self, fn) -> None:
        self.limiter.on_wait = fn

    def close(self) -> None:
        self._api.close()
        self._cdn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
        attempt = 0
        waits = 0
        key = route_key(method, url) if client is self._api else None
        while True:
            if key:
                self.limiter.wait_turn(key)
            try:
                r = client.request(method, url, **kw)
            except BaseException as exc:
                if key:
                    self.limiter.release(key)
                if not isinstance(exc, httpx.TransportError):
                    raise
                if attempt >= len(_RETRY_DELAYS):
                    raise NetworkError(str(exc)) from exc
                self._sleep(_RETRY_DELAYS[attempt])
                attempt += 1
                continue
            if r.status_code == 429 and waits < _MAX_RATE_LIMIT_WAITS:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except ValueError:
                    wait = float(r.headers.get("Retry-After", 1))
                if key:
                    self.limiter.update(key, r.headers, limited_for=wait)
                else:
                    self._sleep(wait)
                    if self.on_wait:
                        self.on_wait(wait)
                waits += 1
                continue
            if key:
                self.limiter.update(key, r.headers)
            if r.status_code >= 500:
                if attempt >= len(_RETRY_DELAYS):
                    raise NetworkError(f"server error {r.status_code}")
                self._sleep(_RETRY_DELAYS[attempt])
                attempt += 1
                continue
            if r.status_code == 401:
                raise DiscordAuthError(r.text)
            if r.status_code == 403:
                raise DiscordPermissionError(r.text)
            if r.status_code == 404:
                raise DiscordNotFound(r.text)
            if r.status_code >= 400:
                raise DiscordError(f"{r.status_code}: {r.text}")
            return r

    def _json(self, method: str, path: str, **kw):
        r = self._send(self._api, method, path, **kw)
        return r.json() if r.content else None

    def me(self) -> dict:
        return self._json("GET", "/users/@me")

    def guild(self, guild_id: str) -> dict:
        return self._json("GET", f"/guilds/{guild_id}")

    def member(self, guild_id: str, user_id: str) -> dict:
        return self._json("GET", f"/guilds/{guild_id}/members/{user_id}")

    def channels(self, guild_id: str) -> list[dict]:
        return self._json("GET", f"/guilds/{guild_id}/channels")

    def create_category(self, guild_id: str, name: str) -> str:
        return self._json("POST", f"/guilds/{guild_id}/channels", json={"name": name, "type": TYPE_CATEGORY})["id"]

    def create_text_channel(self, guild_id: str, name: str, parent_id: str) -> str:
        body = {"name": name, "type": TYPE_TEXT, "parent_id": parent_id}
        return self._json("POST", f"/guilds/{guild_id}/channels", json=body)["id"]

    def delete_channel(self, channel_id: str) -> None:
        self._json("DELETE", f"/channels/{channel_id}")

    def send_message(self, channel_id: str, content: str) -> dict:
        return self._json("POST", f"/channels/{channel_id}/messages", json={"content": content})

    def send_file(self, channel_id: str, content: str, filename: str, data: bytes) -> dict:
        payload = {"content": content, "attachments": [{"id": 0, "filename": filename}]}
        return self._json(
            "POST",
            f"/channels/{channel_id}/messages",
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (filename, data, "application/octet-stream")},
        )

    def create_thread(self, channel_id: str, message_id: str, name: str) -> str:
        body = {"name": name, "auto_archive_duration": 10080}
        return self._json("POST", f"/channels/{channel_id}/messages/{message_id}/threads", json=body)["id"]

    def get_message(self, channel_id: str, message_id: str) -> dict:
        return self._json("GET", f"/channels/{channel_id}/messages/{message_id}")

    def delete_message(self, channel_id: str, message_id: str) -> None:
        self._json("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    def recent_messages(self, channel_id: str, limit: int = 50) -> list[dict]:
        return self._json("GET", f"/channels/{channel_id}/messages", params={"limit": limit})

    def messages_after(self, channel_id: str, after: str = "0", limit: int = 100) -> list[dict]:
        return self._json("GET", f"/channels/{channel_id}/messages", params={"after": after, "limit": limit})

    def download(self, url: str) -> bytes:
        return self._send(self._cdn, "GET", url).content


@dataclass
class ConnectionInfo:
    bot_id: str
    bot_name: str
    guild_name: str
    premium_tier: int
    missing: list[str] = field(default_factory=list)
    has_backup: bool = False
    icon: str | None = None


def check_connection(client, guild_id: str) -> ConnectionInfo:
    me = client.me()
    guild = client.guild(guild_id)
    member = client.member(guild_id, me["id"])
    perms = compute_permissions(guild, member, me["id"])
    info = ConnectionInfo(
        bot_id=me["id"],
        bot_name=me.get("global_name") or me["username"],
        guild_name=guild["name"],
        premium_tier=int(guild.get("premium_tier", 0)),
        missing=missing_permissions(perms),
        icon=guild.get("icon"),
    )
    try:
        info.has_backup = bool(locate_control(client, guild_id, me["id"])[2])
    except DiscordError:
        info.has_backup = False
    return info
