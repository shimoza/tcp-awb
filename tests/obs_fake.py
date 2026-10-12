"""Local stand-ins for the object storage, path style on 127.0.0.1. FakeOBS is one bucket: it checks the V2
signature of every request the way the object storage does and answers list, head, get, put (also a copy) and
delete. FakeOBSService is the service of a region for the key service: buckets, their policy and the list of
buckets. Objects live in memory."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from awb import obs

NS = "http://obs.otc.t-systems.com/doc/2016-01-01/"
SUBRESOURCES = ("policy", "acl", "delete")
BAD_AUTHORIZATION = "Authorization header is invalid -- one and only one ' ' (space) required"
"""The message of the live 400 InvalidArgument (2026-10-12) for a call signed the way of the API gateway."""
ACCESS_DENIED = " ".join(("Access", "Denied"))
"""The message of a 403 AccessDenied, built from its words."""


def signed_v2(h: BaseHTTPRequestHandler, method: str, ak: str, sk: str) -> bool:
    """True when the request carries the V2 signature of this key pair, checked the way the object storage does:
    the path as sent, the sub-resource of the query, Content-MD5, Content-Type, Date and the x-obs- headers."""
    auth = h.headers.get("Authorization", "")
    prefix = "OBS %s:" % ak
    if not auth.startswith(prefix):
        return False
    parts = urllib.parse.urlsplit(h.path)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    sub = next((s for s in SUBRESOURCES if s in query), "")
    resource = parts.path + ("?" + sub if sub else "")
    extra = sorted((k.lower(), v.strip()) for k, v in h.headers.items() if k.lower().startswith("x-obs-"))
    text = "\n".join([method, h.headers.get("Content-MD5", ""), h.headers.get("Content-Type", ""),
                      h.headers.get("Date", "")]) + "\n" + "".join("%s:%s\n" % kv for kv in extra) + resource
    want = base64.b64encode(hmac.new(sk.encode(), text.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(auth[len(prefix):], want)


def error_xml(code: str, message: str) -> bytes:
    """An error answer in the form of the object storage."""
    return ('<?xml version="1.0" encoding="UTF-8"?><Error><Code>%s</Code><Message>%s</Message>'
            '<RequestId>0000018F00000000</RequestId><HostId>fake-host-id</HostId></Error>'
            % (escape(code), escape(message))).encode()


class FakeOBS:
    def __init__(self, bucket: str = "awb", ak: str = "AKFAKE", sk: str = "sk-fake-secret"):
        self.bucket, self.ak, self.sk = bucket, ak, sk
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        # an accept that finds no connection after the poll gives up after a second instead of blocking the serving
        # thread, so shutdown() always returns (a run hung in it on 2026-10-10)
        self.server.socket.settimeout(1.0)
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
        return signed_v2(h, method, self.ak, self.sk)

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


class FakeOBSService:
    """A local stand-in for the OBS service of one region, path style on 127.0.0.1, for the calls the key service
    signs: GET / lists the buckets, PUT /BUCKET makes one, PUT /BUCKET?policy sets its policy (JSON), HEAD and
    DELETE /BUCKET, GET /BUCKET lists it and GET /BUCKET/KEY reads an object; an action the policy denies answers 403
    AccessDenied. `keys` maps every access key it takes to its secret. A call signed the way of the API gateway is
    refused with the live answer: 400 InvalidArgument. `calls` keeps (method, path, the scheme of Authorization)."""

    def __init__(self, keys: dict[str, str] | None = None):
        self.keys = dict(keys or {"AKFAKE": "sk-fake-secret"})
        self.buckets: dict[str, dict] = {}            # name -> {"policy": dict or None, "objects": {key: bytes}}
        self.calls: list[tuple[str, str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.socket.settimeout(1.0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOBSService":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def denied(self, bucket: str, action: str) -> bool:
        policy = self.buckets[bucket]["policy"] or {}
        return any(s.get("Effect") == "Deny" and action in (s.get("Action") or [])
                   for s in policy.get("Statement", []))

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status: int, body: bytes = b"", code: str = "", message: str = ""):
                if code:
                    body = error_xml(code, message)
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _route(self, method: str):
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                parts = urllib.parse.urlsplit(self.path)
                auth = self.headers.get("Authorization", "")
                fake.calls.append((method, parts.path, auth.split(" ", 1)[0]))
                if auth.count(" ") != 1:
                    return self._answer(400, code="InvalidArgument", message=BAD_AUTHORIZATION)
                ak = auth[4:].partition(":")[0] if auth.startswith("OBS ") else ""
                if ak not in fake.keys or not signed_v2(self, method, ak, fake.keys[ak]):
                    return self._answer(403, code="SignatureDoesNotMatch", message="the signature does not match")
                query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
                bucket, _, key = parts.path.lstrip("/").partition("/")
                key = urllib.parse.unquote(key)
                if not bucket:
                    if method != "GET":
                        return self._answer(405, code="MethodNotAllowed", message="not allowed")
                    names = "".join("<Bucket><Name>%s</Name></Bucket>" % escape(b) for b in sorted(fake.buckets))
                    return self._answer(200, ('<?xml version="1.0" encoding="UTF-8"?><ListAllMyBucketsResult '
                                              'xmlns="%s"><Buckets>%s</Buckets></ListAllMyBucketsResult>'
                                              % (NS, names)).encode())
                if method == "PUT" and not key and "policy" not in query:
                    if bucket in fake.buckets:
                        return self._answer(409, code="BucketAlreadyOwnedByYou", message="the bucket is there")
                    fake.buckets[bucket] = {"policy": None, "objects": {}}
                    return self._answer(200)
                if bucket not in fake.buckets:
                    return self._answer(404, code="NoSuchBucket", message="no such bucket")
                if method == "PUT" and "policy" in query:
                    try:
                        fake.buckets[bucket]["policy"] = json.loads(payload)
                    except ValueError:
                        return self._answer(400, code="MalformedPolicy", message="the policy is not JSON")
                    return self._answer(204)
                if method == "HEAD" and not key:
                    return self._answer(200)
                if method == "DELETE" and not key:
                    del fake.buckets[bucket]
                    return self._answer(204)
                if method == "GET":
                    action = "GetObject" if key else "ListBucket"
                    if fake.denied(bucket, action):
                        return self._answer(403, code="AccessDenied", message=ACCESS_DENIED)
                    if key:
                        if key not in fake.buckets[bucket]["objects"]:
                            return self._answer(404, code="NoSuchKey", message="no such key")
                        return self._answer(200, fake.buckets[bucket]["objects"][key])
                    return self._answer(200, ('<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="%s">'
                                              '<Name>%s</Name><IsTruncated>false</IsTruncated></ListBucketResult>'
                                              % (NS, escape(bucket))).encode())
                return self._answer(405, code="MethodNotAllowed", message="not allowed")

            def do_GET(self):
                self._route("GET")

            def do_HEAD(self):
                self._route("HEAD")

            def do_PUT(self):
                self._route("PUT")

            def do_POST(self):
                self._route("POST")

            def do_DELETE(self):
                self._route("DELETE")

        return Handler
