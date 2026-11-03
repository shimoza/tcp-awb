"""A stand-in key service for the lab recipes: one object that answers a request of the call socket the way
awb/tcp/keys.py answers it (ok, status, data, text; a refusal as ok false with an error), over a small invented
cloud kept in memory.

Every path takes the form /v1/{project_id}/<collection>[/<id>[/tags/action]]: a POST to a collection creates an item
under the singular of its name (widgets: widget) with a fresh id and the status BUILD; a GET of an item answers it
(ACTIVE after `polls_to_active` reads) or 404 once it is deleted; a GET of the collection lists the items not deleted
with their id and name (the find by name); a DELETE marks it deleted; a tag call answers 204.
GET /v1/{project_id}/vpcs is the grant probe. GET /v2/{project_id}/types answers the EVS disk types of
`volume_types` (by default SAS and SSD offered in eu-de-01 to eu-de-03 and SATA sold out in all three, the shape
read live on 2026-10-12) or `types_status` when that is set. The knobs: refuse (the probe's refusal), fail_create (collection:
status or "lost" for no answer at all; nothing is made), made_but (collection: status or "lost"; the item is made,
then the answer is that status or none), list_fail (the next so many GETs of a collection answer 500), fail_delete
(collections whose DELETE answers 500), never_active, plant (fields put into every item read), on_call (a function
run before each answer, for a signal), forbidden (a GET of /v1/{project_id}/forbidden answers 403 or this
status), refused_body (the body of a refused create, by default an error_msg alone), refused_text (its text, for
an answer that is not JSON such as the XML of an OBS error) and secrets (tenant alias: the
names of its secrets, which the operation tenants lists as the key service does, never a value). For the RDS recipe the
fake also answers GET /v3/{project_id}/storage-type/SQLServer with `storage_types` (by default CLOUDSSD and GPSSD2
normal and ULTRAHIGH unsupported in eu-de-01 to eu-de-03, the shape read live on 2026-10-17) or `storage_status`
when that is set, GET /v3/{project_id}/instances with the instances made (name, total_count), POST
/v3/{project_id}/instances with `rds_answer` (a function of the body that gives (status, data); by default the 500
and DBS.200005 of a collation RDS does not take) and the vpc subnet delete and the tag call. Every id and value
here is invented.
"""
from __future__ import annotations

import itertools
import json
import re

_ITEM = re.compile(r"^/v1/\{project_id\}/(?P<coll>[a-z-]+)(?:/(?P<id>[a-z0-9-]+)(?P<tags>/tags/action)?)?$")


class FakeKeyService:
    def __init__(self, *, polls_to_active: int = 1):
        self.items: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.refuse = ""
        self.fail_create: dict[str, int | str] = {}
        self.made_but: dict[str, int | str] = {}
        self.list_fail = 0
        self.fail_delete: set[str] = set()
        self.never_active = False
        self.polls_to_active = polls_to_active
        self.plant: dict = {}
        self.forbidden = 403
        self.on_call = None
        self.refused_body: dict | None = {"error_msg": "refused"}
        self.refused_text = ""
        self.secrets: dict[str, list[str]] = {}
        zones = "eu-de-01,eu-de-02,eu-de-03"
        self.volume_types: list[dict] = [
            {"name": n, "id": "%08x-5555-4666-8777-%012x" % (i, 0xd15c), "is_public": True,
             "extra_specs": dict({"RESKEY:availability_zones": zones},
                                 **({"os-vendor-extended:sold_out_availability_zones": zones} if n == "SATA" else {}))}
            for i, n in enumerate(("SAS", "SSD", "SATA"), start=1)]
        self.types_status = 0
        self.rds_answer = lambda body: (500, {"error_code": "DBS.200005", "error_msg": "refused"})
        zones3 = ("eu-de-01", "eu-de-02", "eu-de-03")
        self.storage_types: list[dict] = [
            {"name": n, "az_status": {z: ("unsupported" if n == "ULTRAHIGH" else "normal") for z in zones3}}
            for n in ("ULTRAHIGH", "CLOUDSSD", "GPSSD2")]
        self.storage_status = 0
        self.rds_bodies: list[dict] = []
        self._n = itertools.count(1)

    def created(self) -> list[str]:
        return list(self.items)

    def live(self) -> list[str]:
        return [i for i, it in self.items.items() if not it["deleted"]]

    def __call__(self, req: dict) -> dict:
        self.calls.append(req)
        if self.on_call is not None:
            self.on_call(req)
        if req.get("op") == "tenants":
            return {"ok": True, "tenants": {a: {"roles": ["lab", "read"], "secrets": sorted(names), "regions": []}
                                            for a, names in self.secrets.items()}}
        if req.get("op") != "call":
            return {"ok": False, "error": "unknown operation"}
        if self.refuse:
            return {"ok": False, "error": self.refuse}
        method, path = req["method"], req["path"]
        if method == "GET" and path == "/v1/{project_id}/vpcs":
            return {"ok": True, "status": 200, "data": {"vpcs": []}, "text": ""}
        if method == "GET" and path == "/v2/{project_id}/types":
            if self.types_status:
                return {"ok": True, "status": self.types_status, "data": {"error_msg": "internal"}, "text": ""}
            return {"ok": True, "status": 200, "data": {"volume_types": self.volume_types}, "text": ""}
        if method == "GET" and path == "/v3/{project_id}/storage-type/SQLServer":
            if self.storage_status:
                return {"ok": True, "status": self.storage_status, "data": {"error_msg": "internal"}, "text": ""}
            return {"ok": True, "status": 200, "data": {"storage_type": self.storage_types}, "text": ""}
        if path == "/v3/{project_id}/instances":
            if method == "GET":
                return {"ok": True, "status": 200, "data": {"instances": [], "total_count": 0}, "text": ""}
            if method == "POST":
                self.rds_bodies.append(req.get("body") or {})
                status, data = self.rds_answer(req.get("body") or {})
                return {"ok": True, "status": status, "data": data, "text": ""}
        if method == "DELETE" and re.match(r"^/v1/\{project_id\}/vpcs/[a-z0-9-]+/subnets/[a-z0-9-]+$", path):
            item = self.items.get(path.rsplit("/", 1)[1])
            if item is not None:
                item["deleted"] = True
            return {"ok": True, "status": 204, "data": None, "text": ""}
        if method == "POST" and re.match(r"^/v2\.0/\{project_id\}/vpcs/[a-z0-9-]+/tags/action$", path):
            return {"ok": True, "status": 204, "data": None, "text": ""}
        if method == "GET" and path == "/v1/{project_id}/forbidden":
            return {"ok": True, "status": self.forbidden, "data": {"error_msg": "denied"}, "text": ""}
        m = _ITEM.match(path)
        if not m:
            return {"ok": True, "status": 404, "data": {"error_msg": "no such path"}, "text": ""}
        coll, rid, tags = m.group("coll"), m.group("id"), m.group("tags")
        one = coll[:-1].replace("-", "_")
        if rid is None and method == "POST":
            if coll in self.fail_create:
                if self.fail_create[coll] == "lost":
                    raise ConnectionResetError("no answer")
                return {"ok": True, "status": self.fail_create[coll], "data": self.refused_body,
                        "text": self.refused_text}
            rid = "%08x-0000-4000-8000-%012x" % (0x1a2b3c4d + next(self._n), 0xabcdef0 + len(self.items))
            body = (req.get("body") or {}).get(one) or {}
            self.items[rid] = {"coll": coll, "deleted": False, "reads": 0, "body": body}
            if coll in self.made_but:
                if self.made_but[coll] == "lost":
                    raise ConnectionResetError("no answer")
                return {"ok": True, "status": self.made_but[coll], "data": {"error_msg": "internal"}, "text": ""}
            return {"ok": True, "status": 200, "data": {one: {"id": rid, "status": "BUILD"}}, "text": ""}
        if rid is None and method == "GET":
            if self.list_fail > 0:
                self.list_fail -= 1
                return {"ok": True, "status": 500, "data": {"error_msg": "internal"}, "text": ""}
            items = [{"id": i, "name": it["body"].get("name")} for i, it in self.items.items()
                     if it["coll"] == coll and not it["deleted"]]
            return {"ok": True, "status": 200, "data": {coll: items}, "text": ""}
        item = self.items.get(rid or "")
        if item is None or item["coll"] != coll:
            return {"ok": True, "status": 404, "data": {"error_msg": "not found"}, "text": ""}
        if tags:
            item["tags"] = (req.get("body") or {}).get("tags")
            return {"ok": True, "status": 204, "data": None, "text": ""}
        if method == "DELETE":
            if coll in self.fail_delete:
                return {"ok": True, "status": 500, "data": {"error_msg": "internal"}, "text": ""}
            item["deleted"] = True
            return {"ok": True, "status": 204, "data": None, "text": ""}
        if method == "GET":
            if item["deleted"]:
                return {"ok": True, "status": 404, "data": {"error_msg": "not found"}, "text": ""}
            item["reads"] += 1
            status = "ACTIVE" if item["reads"] >= self.polls_to_active and not self.never_active else "BUILD"
            data = {one: dict({"id": rid, "status": status, "size": "small"}, **self.plant)}
            return {"ok": True, "status": 200, "data": data, "text": ""}
        return {"ok": True, "status": 405, "data": {"error_msg": "method"}, "text": ""}


class Clock:
    """Time that moves only when the run sleeps or a test moves it."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _Answer:
    """The answer of a call of FakeObsLab as labobs._stream gives it: read in blocks, cut after `cut_at` bytes."""

    def __init__(self, data: bytes, cut_at: int | None = None):
        self.data, self.pos, self.cut_at, self.closed = data, 0, cut_at, False

    def read(self, n: int = -1) -> bytes:
        if self.cut_at is not None and self.pos >= self.cut_at:
            raise ConnectionResetError("cut")
        end = len(self.data) if n is None or n < 0 else self.pos + n
        if self.cut_at is not None:
            end = min(end, self.cut_at)
        out = self.data[self.pos:end]
        self.pos += len(out)
        return out

    def close(self) -> None:
        self.closed = True


class FakeObsLab:
    """IAM and OBS of a lab tenant in memory for awb/tcp/labobs.py (task LO), with no socket: `http` answers as
    labadmin._http does (status, headers, bytes), `stream` as labobs._stream does (status, headers, an answer read
    in blocks). IAM takes the admin login `login` (a password login, then a temporary credential of its token); OBS
    checks the V2 signature of every call against the temporary credentials it handed out and keeps buckets,
    objects and multipart uploads. The knobs: cut (part number: times its body breaks half way), down (every part
    and every get breaks), hold (part number: an Event the part waits for; `holding` is set while it waits),
    get_cut (gets that break half way) and virtual ((bucket, key): size of an object only a head answers).
    `calls` lists (method, path, query) of the OBS calls, `aks` the access key of each, `parts` the part numbers
    sent and `ranges` the Range header of each get. Every value here is invented."""

    def __init__(self, login: dict):
        import threading

        self.login = login
        self.tokens: set[str] = set()
        self.creds: dict[str, tuple[str, str]] = {}
        self.buckets: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.aks: list[str] = []
        self.parts: list[int] = []
        self.cut: dict[int, int] = {}
        self.down = False
        self.hold: dict[int, object] = {}
        self.holding = threading.Event()
        self.get_cut = 0
        self.virtual: dict[tuple[str, str], int] = {}
        self.ranges: list[str | None] = []
        self._n = itertools.count(1)

    # the entry points of the service's two senders
    def http(self, method, url, headers, data, timeout=None):
        status, hdrs, body = self.handle(method, url, headers, data)
        return status, hdrs, body.read() if isinstance(body, _Answer) else body

    def stream(self, method, url, headers, body=None, timeout=None):
        status, hdrs, out = self.handle(method, url, headers, body)
        return status, hdrs, out if isinstance(out, _Answer) else _Answer(out)

    # the platform
    def handle(self, method, url, headers, body):
        import urllib.parse

        parts = urllib.parse.urlsplit(url)
        path = parts.path
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        hdrs = {k.lower(): v for k, v in headers.items()}
        if path == "/v3/auth/tokens":
            return self._login(json.loads(body))
        if path == "/v3.0/OS-CREDENTIAL/securitytokens":
            if hdrs.get("x-auth-token") not in self.tokens:
                return 401, {}, b'{"error": {"code": "IAM.0067"}}'
            n = next(self._n)
            ak, sk, st = "TMPAK%015d" % n, "tmpsk" + "q" * 35 + str(n), "gQ" + "w" * 300 + str(n)
            self.creds[ak] = (sk, st)
            return 201, {}, json.dumps({"credential": {"access": ak, "secret": sk, "securitytoken": st,
                                                       "expires_at": "2099-01-01T00:00:00.000000Z"}}).encode()
        bad = self._signature(method, path, query, headers)
        if bad:
            return 403, {}, self._error(bad)
        self.calls.append((method, path, query))
        segs = urllib.parse.unquote(path).lstrip("/").split("/", 1)
        bucket, key = segs[0], segs[1] if len(segs) > 1 else ""
        return self._obs(method, bucket, key, query, hdrs, body)

    def _login(self, req):
        user = req["auth"]["identity"]["password"]["user"]
        if (user["name"], user["password"], user["domain"]["name"]) != (self.login["user"], self.login["password"],
                                                                         self.login["domain"]):
            return 401, {}, b'{"error": {"code": "IAM.0062"}}'
        token = "MII%05d" % next(self._n) + "z" * 90
        self.tokens.add(token)
        body = {"token": {"expires_at": "2099-01-01T00:00:00.000000Z", "domain": {"id": "d" * 32},
                          "project": {"id": "e" * 32, "domain": {"id": "d" * 32}}}}
        return 201, {"X-Subject-Token": token}, json.dumps(body).encode()

    def _signature(self, method, path, query, headers) -> str:
        """The error code of a call whose V2 signature or security token is not one of a credential handed out."""
        import base64
        import hashlib
        import hmac

        from awb import obs

        auth = headers.get("Authorization") or ""
        if not auth.startswith("OBS ") or ":" not in auth:
            return "AccessDenied"
        ak, _, sig = auth[4:].partition(":")
        self.aks.append(ak)
        if ak not in self.creds or headers.get("x-obs-security-token") != self.creds[ak][1]:
            return "InvalidAccessKeyId"
        lower = {k.lower(): v for k, v in headers.items()}
        sub = sorted((k, v) for k, v in query.items() if k in obs.SUBRESOURCES)
        resource = path + ("?" + "&".join(k + ("=" + v if v else "") for k, v in sub) if sub else "")
        text = "\n".join([method, lower.get("content-md5", ""), lower.get("content-type", ""), lower.get("date", "")])
        text += "\n" + "".join("%s:%s\n" % (k, str(v).strip()) for k, v in sorted(lower.items())
                               if k.startswith("x-obs-")) + resource
        mac = base64.b64encode(hmac.new(self.creds[ak][0].encode(), text.encode(), hashlib.sha1).digest()).decode()
        return "" if hmac.compare_digest(mac, sig) else "SignatureDoesNotMatch"

    @staticmethod
    def _error(code: str) -> bytes:
        return ("<?xml version=\"1.0\"?><Error><Code>%s</Code><Message>no</Message></Error>" % code).encode()

    @staticmethod
    def _read(body) -> bytes:
        if body is None:
            return b""
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        out = bytearray()
        while True:
            chunk = body.read(8192)
            if not chunk:
                return bytes(out)
            out += chunk

    def _obs(self, method, bucket, key, query, hdrs, body):
        import hashlib

        b = self.buckets.get(bucket)
        if not key:
            if method == "PUT":
                self.buckets.setdefault(bucket, {"objects": {}, "uploads": {}})
                return 200, {}, b""
            if b is None:
                return 404, {}, self._error("NoSuchBucket")
            if method == "DELETE":
                if b["objects"] or b["uploads"]:
                    return 409, {}, self._error("BucketNotEmpty")
                del self.buckets[bucket]
                return 204, {}, b""
            if method == "GET" and "uploads" in query:
                ups = "".join("<Upload><Key>%s</Key><UploadId>%s</UploadId></Upload>" % (u["key"], uid)
                              for uid, u in b["uploads"].items())
                return 200, {}, ("<ListMultipartUploadsResult><Bucket>%s</Bucket>%s<IsTruncated>false</IsTruncated>"
                                 "</ListMultipartUploadsResult>" % (bucket, ups)).encode()
            if method == "GET":
                keys = "".join("<Contents><Key>%s</Key><Size>%d</Size></Contents>" % (k, len(o["data"]))
                               for k, o in b["objects"].items())
                return 200, {}, ("<ListBucketResult><Name>%s</Name>%s<IsTruncated>false</IsTruncated>"
                                 "</ListBucketResult>" % (bucket, keys)).encode()
            return 200, {}, b""
        if b is None:
            return 404, {}, self._error("NoSuchBucket")
        if "uploadId" in query:
            up = b["uploads"].get(query["uploadId"])
            if up is None or up["key"] != key:
                self._read(body)
                return 404, {}, self._error("NoSuchUpload")
            if method == "PUT":
                n = int(query["partNumber"])
                self.parts.append(n)
                if n in self.hold:
                    self.holding.set()
                    self.hold.pop(n).wait(10)
                if self.down or self.cut.get(n):
                    if self.cut.get(n):
                        self.cut[n] -= 1
                    body.read(max(1, int(hdrs.get("content-length", "2")) // 2))
                    raise ConnectionResetError("cut")
                data = self._read(body)
                if len(data) != int(hdrs.get("content-length", -1)):
                    return 400, {}, self._error("IncompleteBody")
                etag = '"%s"' % hashlib.md5(data).hexdigest()
                up["parts"][n] = (data, etag)
                return 200, {"ETag": etag}, b""
            if method == "POST":
                want = [(int(n), e) for n, e in re.findall(r"<PartNumber>(\d+)</PartNumber><ETag>([^<]+)</ETag>",
                                                           self._read(body).decode())]
                if [n for n, _ in want] != sorted(up["parts"]) or any(up["parts"][n][1] != e.replace("&quot;", '"')
                                                                      for n, e in want):
                    return 400, {}, self._error("InvalidPart")
                data = b"".join(up["parts"][n][0] for n, _ in want)
                b["objects"][key] = {"data": data, "meta": up["meta"],
                                     "etag": '"%s-%d"' % (hashlib.md5(data).hexdigest(), len(want))}
                del b["uploads"][query["uploadId"]]
                return 200, {}, ("<CompleteMultipartUploadResult><Key>%s</Key></CompleteMultipartUploadResult>"
                                 % key).encode()
            if method == "DELETE":
                del b["uploads"][query["uploadId"]]
                return 204, {}, b""
        if method == "POST" and "uploads" in query:
            uid = "UPL%012d" % next(self._n)
            b["uploads"][uid] = {"key": key, "parts": {}, "meta": {k: v for k, v in hdrs.items()
                                                                   if k.startswith("x-obs-meta-")}}
            return 200, {}, ("<InitiateMultipartUploadResult><Bucket>%s</Bucket><Key>%s</Key><UploadId>%s</UploadId>"
                             "</InitiateMultipartUploadResult>" % (bucket, key, uid)).encode()
        obj = b["objects"].get(key)
        if method == "HEAD":
            if (bucket, key) in self.virtual:
                return 200, {"Content-Length": str(self.virtual[(bucket, key)]), "ETag": '"v1"'}, b""
            if obj is None:
                return 404, {}, b""
            return 200, dict({"Content-Length": str(len(obj["data"])), "ETag": obj["etag"]}, **obj["meta"]), b""
        if method == "GET":
            if obj is None:
                return 404, {}, self._error("NoSuchKey")
            if hdrs.get("if-match") and hdrs["if-match"] != obj["etag"]:
                return 412, {}, self._error("PreconditionFailed")
            self.ranges.append(hdrs.get("range"))
            start = int(hdrs["range"][6:].rstrip("-")) if hdrs.get("range") else 0
            data = obj["data"][start:]
            cut = None
            if self.down or self.get_cut > 0:
                self.get_cut = max(0, self.get_cut - 1)
                cut = len(data) // 2
            return (206 if start else 200), {"Content-Length": str(len(data)), "ETag": obj["etag"]}, \
                _Answer(data, cut)
        if method == "DELETE":
            b["objects"].pop(key, None)
            return 204, {}, b""
        if method == "PUT":
            b["objects"][key] = {"data": self._read(body), "meta": {}, "etag": '"x"'}
            return 200, {}, b""
        return 405, {}, b""
