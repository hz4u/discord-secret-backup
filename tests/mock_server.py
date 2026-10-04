import email.parser
import json
import re
import threading
import time

import httpx

from tests.fake_discord import FakeDiscord

CDN = "https://cdn.test/"
MESSAGE_BUCKET = "80c17d2f203122d936070c88c8d10f33"


class MockDiscordServer:
    def __init__(self, fake: FakeDiscord | None = None, per_channel=(5, 5.0), upload_bps: float | None = None,
                 latency: float = 0.0):
        self.fake = fake or FakeDiscord()
        self.limit, self.window = per_channel
        self.upload_bps = upload_bps
        self.latency = latency
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._uplink = threading.Lock()
        self.rate_limited = 0
        self.wasted_bytes = 0
        self.sent_bytes = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _transfer(self, n: int) -> None:
        self.sent_bytes += n
        if self.upload_bps and n:
            with self._uplink:
                time.sleep(n / self.upload_bps)

    def _take(self, channel_id: str) -> tuple[bool, int, float]:
        now = time.monotonic()
        with self._lock:
            start, count = self._buckets.get(channel_id, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            reset_after = self.window - (now - start)
            if count >= self.limit:
                self._buckets[channel_id] = [start, count]
                return False, 0, reset_after
            count += 1
            self._buckets[channel_id] = [start, count]
            return True, self.limit - count, reset_after

    @staticmethod
    def _json(status: int, body, headers=None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {})

    def _cdn_url(self, msg: dict) -> dict:
        msg = dict(msg)
        msg["attachments"] = [{**a, "url": CDN + a["url"].removeprefix("fake://")} for a in msg.get("attachments", [])]
        return msg

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.latency:
            time.sleep(self.latency)
        url = str(req.url)
        if url.startswith(CDN):
            return httpx.Response(200, content=self.fake.download("fake://" + url.removeprefix(CDN)))
        path = req.url.path.removeprefix("/api/v10")
        body = req.read()
        f = self.fake

        m = re.fullmatch(r"/channels/(\d+)/messages", path)
        if m and req.method == "POST":
            cid = m.group(1)
            self._transfer(len(body))
            ok, remaining, reset_after = self._take(cid)
            headers = {"X-RateLimit-Limit": str(self.limit), "X-RateLimit-Remaining": str(remaining),
                       "X-RateLimit-Reset-After": f"{reset_after:.3f}", "X-RateLimit-Bucket": MESSAGE_BUCKET}
            if not ok:
                self.rate_limited += 1
                self.wasted_bytes += len(body)
                return self._json(429, {"message": "You are being rate limited.", "retry_after": round(reset_after, 3),
                                        "global": False}, headers)
            ctype = req.headers.get("content-type", "")
            if ctype.startswith("multipart/"):
                parsed = email.parser.BytesParser().parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
                payload, data, filename = {}, b"", "file.bin"
                for part in parsed.get_payload():
                    name = part.get_param("name", header="content-disposition")
                    if name == "payload_json":
                        payload = json.loads(part.get_payload(decode=True))
                    else:
                        data = part.get_payload(decode=True)
                        filename = part.get_param("filename", header="content-disposition") or filename
                msg = f.send_file(cid, payload.get("content", ""), filename, data)
            else:
                msg = f.send_message(cid, json.loads(body).get("content", ""))
            return self._json(200, self._cdn_url(msg), headers)
        if m and req.method == "GET":
            params = dict(req.url.params)
            if "after" in params:
                msgs = f.messages_after(m.group(1), params["after"], int(params.get("limit", 100)))
            else:
                msgs = f.recent_messages(m.group(1), int(params.get("limit", 50)))
            return self._json(200, [self._cdn_url(x) for x in msgs])
        m = re.fullmatch(r"/channels/(\d+)/messages/(\d+)", path)
        if m:
            if req.method == "DELETE":
                f.delete_message(*m.groups())
                return httpx.Response(204)
            return self._json(200, self._cdn_url(f.get_message(*m.groups())))
        m = re.fullmatch(r"/channels/(\d+)/messages/(\d+)/threads", path)
        if m:
            return self._json(200, {"id": f.create_thread(m.group(1), m.group(2), json.loads(body)["name"])})
        m = re.fullmatch(r"/channels/(\d+)", path)
        if m and req.method == "DELETE":
            f.delete_channel(m.group(1))
            return httpx.Response(204)
        m = re.fullmatch(r"/guilds/(\d+)/channels", path)
        if m and req.method == "POST":
            b = json.loads(body)
            cid = (f.create_category(m.group(1), b["name"]) if b["type"] == 4
                   else f.create_text_channel(m.group(1), b["name"], b.get("parent_id")))
            return self._json(200, {"id": cid})
        if m:
            return self._json(200, f.channels(m.group(1)))
        m = re.fullmatch(r"/guilds/(\d+)", path)
        if m:
            return self._json(200, f.guild(m.group(1)))
        if path == "/users/@me":
            return self._json(200, f.me())
        return self._json(404, {"message": f"unknown route {req.method} {path}"})
