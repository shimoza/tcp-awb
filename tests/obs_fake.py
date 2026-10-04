"""A local stand-in for one OBS bucket, path style on 127.0.0.1: it checks the V2 signature of every request the
way the object storage does and answers list, head, get, put (also a copy) and delete. Objects live in memory."""
from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from awb import obs

NS = "http://obs.otc.t-systems.com/doc/2016-01-01/"
SUBRESOURCES = ("policy", "acl", "delete")


class FakeOBS:
    def __init__(self, bucket: str = "awb", ak: str = "AKFAKE", sk: str = "sk-fake-secret"):
        self.bucket, self.ak, self.sk = bucket, ak, sk
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOBS":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def keys(self) -> obs.Keys:
        return obs.Keys(self.ak, self.sk)

    def client(self, **kw) -> obs.Client:
        return obs.Client(self.bucket, kw.pop("keys", self.keys()), "eu-de", endpoint=self.endpoint, **kw)

    # -- the server side

    def _signed(self, h: BaseHTTPRequestHandler, method: str) -> bool:
        auth = h.headers.get("Authorization", "")
        prefix = "OBS %s:" % self.ak
        if not auth.startswith(prefix):
            return False
        parts = urllib.parse.urlsplit(h.path)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        sub = next((s for s in SUBRESOURCES if s in query), "")
        resource = parts.path + ("?" + sub if sub else "")
        extra = sorted((k.lower(), v.strip()) for k, v in h.headers.items() if k.lower().startswith("x-obs-"))
        text = "\n".join([method, h.headers.get("Content-MD5", ""), h.headers.get("Content-Type", ""),
                          h.headers.get("Date", "")]) + "\n" + "".join("%s:%s\n" % kv for kv in extra) + resource
        want = base64.b64encode(hmac.new(self.sk.encode(), text.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(auth[len(prefix):], want)

    def _listing(self, query: dict) -> bytes:
        prefix = query.get("prefix", [""])[0]
        delimiter = query.get("delimiter", [""])[0]
        marker = query.get("marker", [""])[0]
        limit = int(query.get("max-keys", ["1000"])[0])
        items: list[tuple[str, str]] = []           # ("key" or "prefix", value)
        seen: set[str] = set()
        truncated = False
        for key in sorted(self.objects):
            if not key.startswith(prefix) or key <= marker:
                continue
            if marker.endswith(delimiter or "\0") and delimiter and key.startswith(marker):
                continue
            rest = key[len(prefix):]
            if delimiter and delimiter in rest:
                common = prefix + rest[:rest.index(delimiter) + len(delimiter)]
                if common in seen:
                    continue
                if len(items) == limit:
                    truncated = True
                    break
                seen.add(common)
                items.append(("prefix", common))
            else:
                if len(items) == limit:
                    truncated = True
                    break
                items.append(("key", key))
        body = ['<?xml version="1.0" encoding="UTF-8"?>', '<ListBucketResult xmlns="%s">' % NS,
                "<Name>%s</Name><Prefix>%s</Prefix><Marker>%s</Marker><MaxKeys>%d</MaxKeys>"
                % (self.bucket, escape(prefix), escape(marker), limit),
                "<IsTruncated>%s</IsTruncated>" % ("true" if truncated else "false")]
        if truncated and delimiter:
            body.append("<NextMarker>%s</NextMarker>" % escape(items[-1][1]))
        for kind, value in items:
            if kind == "key":
                data = self.objects[value]
                body.append('<Contents><Key>%s</Key><Size>%d</Size><ETag>"%s"</ETag>'
                            '<LastModified>%s</LastModified></Contents>'
                            % (escape(value), len(data), hashlib.md5(data).hexdigest(),
                               self.modified.get(value, "2026-10-03T07:58:12.000Z")))
            else:
                body.append("<CommonPrefixes><Prefix>%s</Prefix></CommonPrefixes>" % escape(value))
        body.append("</ListBucketResult>")
        return "".join(body).encode("utf-8")

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status: int, body: bytes = b"", code: str = "", headers: dict | None = None):
                if code:
                    body = ('<?xml version="1.0" encoding="UTF-8"?><Error><Code>%s</Code></Error>' % code).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _route(self, method: str):
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                parts = urllib.parse.urlsplit(self.path)
                fake.calls.append((method, parts.path))
                if not fake._signed(self, method):
                    return self._answer(403, code="SignatureDoesNotMatch")
                segments = parts.path.lstrip("/").split("/", 1)
                if segments[0] != fake.bucket:
                    return self._answer(404, code="NoSuchBucket")
                key = urllib.parse.unquote(segments[1]) if len(segments) > 1 else ""
                if method == "GET" and not key:
                    return self._answer(200, fake._listing(urllib.parse.parse_qs(parts.query)))
                if method in ("GET", "HEAD"):
                    if key not in fake.objects:
                        return self._answer(404, code="NoSuchKey")
                    data = fake.objects[key]
                    etag = hashlib.md5(data).hexdigest()
                    wanted = self.headers.get("If-Match")
                    if wanted and wanted.strip().strip('"') != etag:
                        return self._answer(412, code="PreconditionFailed")
                    return self._answer(200, data, headers={"ETag": '"%s"' % etag})
                if method == "PUT":
                    source = self.headers.get("x-obs-copy-source")
                    if source:
                        src = urllib.parse.unquote(source.split("/", 2)[2])
                        if src not in fake.objects:
                            return self._answer(404, code="NoSuchKey")
                        fake.objects[key] = fake.objects[src]
                    else:
                        fake.objects[key] = payload
                    return self._answer(200)
                if method == "DELETE":
                    fake.objects.pop(key, None)
                    return self._answer(204)
                return self._answer(405, code="MethodNotAllowed")

            def do_GET(self):
                self._route("GET")

            def do_HEAD(self):
                self._route("HEAD")

            def do_PUT(self):
                self._route("PUT")

            def do_DELETE(self):
                self._route("DELETE")

        return Handler
