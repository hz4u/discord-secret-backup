import itertools
import threading

from secret.core.chunks import upload_limit_for_tier
from secret.core.discord_api import TYPE_CATEGORY, TYPE_TEXT, DiscordError, DiscordNotFound, NetworkError

TYPE_THREAD = 11
BOT_ID = "1"
USER_ID = "2"


class FakeDiscord:
    def __init__(self, guild_id="900", premium_tier=0):
        self.guild_id = guild_id
        self.premium_tier = premium_tier
        self._ids = itertools.count(1000)
        self._lock = threading.Lock()
        self.channels_by_id: dict[str, dict] = {}
        self.messages: dict[str, dict[str, dict]] = {}
        self.blobs: dict[str, bytes] = {}
        self.fail_after_uploads: int | None = None
        self.on_download = None
        self.uploads = 0
        self.closed = False

    def _id(self) -> str:
        with self._lock:
            return str(next(self._ids))

    def post_as_user(self, channel_id: str, content: str) -> str:
        msg = {"id": self._id(), "content": content, "author": {"id": USER_ID}, "attachments": []}
        self.messages.setdefault(channel_id, {})[msg["id"]] = msg
        return msg["id"]

    def by_name(self, name: str) -> dict:
        return next(c for c in self.channels_by_id.values() if c["name"] == name)

    def children(self, parent_id: str, type_=None) -> list[dict]:
        return [c for c in self.channels_by_id.values() if c.get("parent_id") == parent_id and (type_ is None or c["type"] == type_)]

    def all_messages(self) -> list[dict]:
        return [m for msgs in self.messages.values() for m in msgs.values()]

    def close(self):
        self.closed = True

    def me(self):
        return {"id": BOT_ID, "username": "secret-bot"}

    def guild(self, guild_id):
        if guild_id != self.guild_id:
            raise DiscordNotFound("unknown guild")
        return {
            "id": guild_id, "name": "테스트 서버", "owner_id": USER_ID, "premium_tier": self.premium_tier,
            "roles": [{"id": guild_id, "permissions": "0"}, {"id": "50", "permissions": str(1 << 3)}],
        }

    def member(self, guild_id, user_id):
        return {"roles": ["50"]}

    def channels(self, guild_id):
        return [dict(c) for c in self.channels_by_id.values() if c["type"] != TYPE_THREAD]

    def _create(self, name, type_, parent_id=None, **extra):
        cid = self._id()
        self.channels_by_id[cid] = {"id": cid, "name": name, "type": type_, "parent_id": parent_id, **extra}
        self.messages[cid] = {}
        return cid

    def create_category(self, guild_id, name):
        return self._create(name, TYPE_CATEGORY)

    def create_text_channel(self, guild_id, name, parent_id):
        if parent_id not in self.channels_by_id:
            raise DiscordNotFound("unknown parent")
        return self._create(name, TYPE_TEXT, parent_id)

    def delete_channel(self, channel_id):
        ch = self.channels_by_id.pop(channel_id, None)
        if ch is None:
            raise DiscordNotFound("unknown channel")
        self.messages.pop(channel_id, None)
        if ch["type"] == TYPE_TEXT:
            for t in self.children(channel_id, TYPE_THREAD):
                self.delete_channel(t["id"])
        elif ch["type"] == TYPE_CATEGORY:
            for c in self.children(channel_id):
                c["parent_id"] = None

    def _channel(self, channel_id):
        if channel_id not in self.channels_by_id:
            raise DiscordNotFound("unknown channel")
        return self.messages[channel_id]

    def send_message(self, channel_id, content):
        msgs = self._channel(channel_id)
        msg = {"id": self._id(), "content": content, "author": {"id": BOT_ID}, "attachments": []}
        msgs[msg["id"]] = msg
        return dict(msg)

    def send_file(self, channel_id, content, filename, data):
        msgs = self._channel(channel_id)
        if len(data) > upload_limit_for_tier(self.premium_tier):
            raise DiscordError("413: request entity too large")
        with self._lock:
            if self.fail_after_uploads is not None and self.uploads >= self.fail_after_uploads:
                raise NetworkError("simulated network failure")
            self.uploads += 1
        att_id = self._id()
        url = f"fake://{att_id}/{filename}"
        self.blobs[url] = bytes(data)
        msg = {
            "id": self._id(), "content": content, "author": {"id": BOT_ID},
            "attachments": [{"id": att_id, "filename": filename, "size": len(data), "url": url}],
        }
        msgs[msg["id"]] = msg
        return dict(msg)

    def create_thread(self, channel_id, message_id, name):
        msgs = self._channel(channel_id)
        if message_id not in msgs:
            raise DiscordNotFound("unknown message")
        tid = self._create(name, TYPE_THREAD, channel_id, message_id=message_id)
        msgs[message_id]["thread"] = {"id": tid, "name": name}
        return tid

    def threads(self) -> list[dict]:
        return [c for c in self.channels_by_id.values() if c["type"] == TYPE_THREAD]

    def get_message(self, channel_id, message_id):
        msg = self._channel(channel_id).get(message_id)
        if msg is None:
            raise DiscordNotFound("unknown message")
        return dict(msg)

    def delete_message(self, channel_id, message_id):
        if self._channel(channel_id).pop(message_id, None) is None:
            raise DiscordNotFound("unknown message")

    def recent_messages(self, channel_id, limit=50):
        msgs = sorted(self._channel(channel_id).values(), key=lambda m: int(m["id"]), reverse=True)
        return [dict(m) for m in msgs[:limit]]

    def messages_after(self, channel_id, after="0", limit=100):
        msgs = sorted(self._channel(channel_id).values(), key=lambda m: int(m["id"]))
        msgs = [m for m in msgs if int(m["id"]) > int(after)][:limit]
        return [dict(m) for m in reversed(msgs)]

    def download(self, url):
        if self.on_download:
            self.on_download(url)
        if url not in self.blobs:
            raise DiscordNotFound("unknown attachment")
        return self.blobs[url]
