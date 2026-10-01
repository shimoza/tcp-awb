"""Local stand-ins for two TCP services, on 127.0.0.1.

FakePriceAPI answers like the public price API: parameters sn or serviceName, rn or region, limitMax, limitFrom;
HTTP 500 with code Error for a service name it does not know; `result` as a dict of lists, or an empty list when
nothing matches. The rates are invented.

FakeGateway checks the SDK-HMAC-SHA256 signature of every request with a verifier of its own (written from the
algorithm, not from awb/sign.py) and answers a few read-only paths: the projects of IAM, a list with marker paging,
a list with offset paging, an empty list, an answer without the list, a server error and a busy service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRICE_PATH = "/en/open-telekom-price-api/"


def record(rid: str, *, service: str = "ecs", flavor: str = "s3.large.2", name: str = "General Purpose s3.large.2",
           os: str = "Open Linux", payg: str = "0.085000", unit: str = "h", region: str = "eu-de",
           r12: str = "45.000000", r24: str = "40.000000", r36: str = "0.000000", tier: str = "",
           tier_from=1, tier_up_to=999999999999) -> dict:
    """One record in the form of the price API; every amount as "<number> EUR"."""
    eur = lambda v: "%s EUR" % v  # noqa: E731
    return {"id": rid, "idGroupTiered": tier, "productId": service.upper(), "opiFlavour": flavor,
            "productName": name, "osUnit": os, "currency": "EUR", "priceAmount": eur(payg), "unit": unit,
            "description": name, "vCpu": "2", "ram": "8 GiB", "additionalText": "", "storageType": "",
            "storageVolume": "", "serviceType": "", "productIdParameter": service, "productSection": "main",
            "productType": "OTC", "productFamily": "Compute", "productCategory": "", "fromOn": tier_from,
            "upTo": tier_up_to, "minAmount": 1, "maxAmount": 999999999999, "region": region, "isMRC": False,
            "R12": eur(r12), "R24": eur(r24), "R36": eur(r36), "RU12": eur("0.000000"), "RU24": eur("0.000000"),
            "RU36": eur("0.000000"), "_idGroup": rid}


def default_records() -> list[dict]:
    return [
        record("TEST_S3_LARGE_2_LNX"),
        record("TEST_S3_LARGE_2_WIN", os="Windows", payg="0.131000", r12="70.000000", r24="62.000000"),
        record("TEST_S3_XLARGE_2_LNX", flavor="s3.xlarge.2", name="General Purpose s3.xlarge.2", payg="0.170000",
               r12="90.000000", r24="80.000000", r36="70.000000"),
        record("TEST_EVS_SSD", service="evs", flavor="evs.ssd", name="EVS Ultra-high I/O", os="Standard",
               payg="0.120000", unit="GB/month", r12="0.000000", r24="0.000000"),
        record("TEST_NL_S3_LARGE_2_LNX", region="eu-nl", payg="0.090000"),
        record("TEST_BW_1", service="vpc", flavor="bandwidth", name="Bandwidth tier 1", os="Standard",
               payg="0.030000", unit="Mbit/h", r12="0.000000", r24="0.000000", tier="TEST_BW", tier_from=0,
               tier_up_to=5),
        record("TEST_BIG", service="dws", flavor="dws.big", name="Warehouse big", os="Standard",
               payg="1,234.500000", unit="h", r12="0.000000", r24="0.000000"),
    ]


class _Server:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]


class FakePriceAPI(_Server):
    """`records` can be changed between calls. `cap` limits the records per page. `mode`: None, "html" (an answer
    that is not JSON), "short" (a count one higher than the records sent), "busy-once" (503, then normal)."""

    def __init__(self, records: list[dict] | None = None, *, cap: int = 10000, cached_at: str | None = None):
        self.records = records if records is not None else default_records()
        self.cap = cap
        self.cached_at = cached_at
        self.mode: str | None = None
        self.calls: list[dict] = []
        self.stray: dict | None = None          # a record added under the result of the service asked for
        super().__init__(self._handler())

    @property
    def url(self) -> str:
        return self.base + PRICE_PATH

    def answer(self, query: dict) -> tuple[int, bytes]:
        self.calls.append(query)
        if self.mode == "busy-once":
            self.mode = None
            return 503, b"busy"
        if self.mode == "html":
            return 200, b"<html>maintenance</html>"
        sn = query.get("sn") or query.get("serviceName")
        rn = query.get("rn") or query.get("region")
        known = {r["productIdParameter"] for r in self.records} | {"dws", "evs", "vpc", "ecs"}
        params = {k: v for k, v in query.items()}
        if sn and sn not in known:
            body = {"response": {"httpCode": 500, "code": "Error", "message": "Unknown service", "parameters": params,
                                 "result": []}}
            return 500, json.dumps(body).encode()
        rows = [r for r in self.records if (not sn or r["productIdParameter"] == sn) and (not rn or r["region"] == rn)]
        if self.stray and sn:
            rows.append(dict(self.stray))
        per = min(int(query.get("limitMax", "500")), self.cap)
        start = int(query.get("limitFrom", "0"))
        page = rows[start:start + per]
        result: dict | list = {}
        for r in page:
            key = sn if (sn and self.stray) else r["productIdParameter"]
            result.setdefault(key, []).append(r)
        count = len(rows) + (1 if self.mode == "short" else 0)
        resp = {"httpCode": 200, "code": "Success", "message": "Product data successfully loaded!",
                "parameters": params, "stats": {"count": count, "recordsCount": len(page),
                                                "maxPages": max(1, -(-len(rows) // per)), "recordsPerPage": per,
                                                "currentPage": start // per + 1},
                "result": result if page else []}
        if self.cached_at:
            resp["cachedAt"] = self.cached_at
        return 200, json.dumps({"response": resp}).encode()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != PRICE_PATH:
                    status, body = 404, b"not found"
                else:
                    status, body = fake.answer(dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True)))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


# --------------------------------------------------------------------------- the API gateway


def verify(method: str, raw_path: str, headers, body: bytes, ak: str, sk: str) -> bool:
    """The SDK-HMAC-SHA256 check of an API gateway, written from the algorithm."""
    auth = headers.get("Authorization", "")
    prefix = "SDK-HMAC-SHA256 "
    if not auth.startswith(prefix):
        return False
    fields = {}
    for part in auth[len(prefix):].split(","):
        k, _, v = part.strip().partition("=")
        fields[k] = v
    if fields.get("Access") != ak or not fields.get("SignedHeaders") or not fields.get("Signature"):
        return False
    path, _, query = raw_path.partition("?")
    segments = [urllib.parse.quote(urllib.parse.unquote(s), safe="~-._") for s in path.split("/")]
    uri = "/".join(segments)
    if uri[-1:] != "/":
        uri = uri + "/"
    pairs = sorted((urllib.parse.quote(k, safe="~-._"), urllib.parse.quote(v, safe="~-._"))
                   for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True))
    canonical_query = "&".join(k + "=" + v for k, v in pairs)
    names = fields["SignedHeaders"].split(";")
    if "host" not in names or "x-sdk-date" not in names:
        return False
    lines = []
    for name in names:
        value = headers.get(name)
        if value is None:
            return False
        lines.append(name + ":" + value.strip())
    canonical = method + "\n" + uri + "\n" + canonical_query + "\n" + "\n".join(lines) + "\n\n" + \
        ";".join(names) + "\n" + hashlib.sha256(body).hexdigest()
    to_sign = "SDK-HMAC-SHA256\n" + headers.get("X-Sdk-Date", "") + "\n" + \
        hashlib.sha256(canonical.encode()).hexdigest()
    want = hmac.new(sk.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, fields["Signature"])


class FakeGateway(_Server):
    PROJECT = "fake-project-of-eu-de"

    def __init__(self, ak: str = "AKFAKE", sk: str = "sk-fake-secret", region: str = "eu-de"):
        self.ak, self.sk, self.region = ak, sk, region
        self.vpcs = [{"id": "vpc-%02d" % i, "name": "net-%02d" % i} for i in range(7)]
        self.ports = [{"id": "port-%02d" % i} for i in range(5)]
        self.busy = 0
        self.throttled = 0
        self.retry_after = ""
        self.echo_key = False
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []
        super().__init__(self._handler())

    def route(self, method: str, raw_path: str) -> tuple[int, dict | str]:
        path, _, query = raw_path.partition("?")
        q = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        pid = self.PROJECT
        if method != "GET":
            return 405, {"error_msg": "read only"}
        if path == "/v3/projects":
            projects = [{"id": pid, "name": self.region}, {"id": "a" * 32, "name": self.region + "_sub"},
                        {"id": "b" * 32, "name": "eu-nl"}]
            if "name" in q:
                projects = [p for p in projects if p["name"] == q["name"]]
            return 200, {"projects": projects}
        if path == "/v1/%s/vpcs" % pid:
            limit = int(q.get("limit", "100"))
            items = [v for v in self.vpcs if v["id"] > q.get("marker", "")][:limit]
            return 200, {"vpcs": items}
        if path == "/v1/%s/ports" % pid:
            limit, offset = int(q.get("limit", "100")), int(q.get("offset", "0"))
            return 200, {"ports": self.ports[offset:offset + limit]}
        if path == "/v1/%s/empty" % pid:
            return 200, {"items": []}
        if path == "/v1/%s/odd" % pid:
            return 200, {"other": 1}
        if path == "/v1/%s/boom" % pid:
            return 500, {"error_msg": "internal"}
        if path == "/v1/%s/throttled" % pid:
            if self.throttled > 0:
                self.throttled -= 1
                return 429, {"error_code": "Common.1503", "error_msg": "API flow control error"}
            return 200, {"items": [{"id": "y"}]}
        if path == "/v1/%s/busy" % pid:
            if self.busy > 0:
                self.busy -= 1
                return 503, "busy"
            return 200, {"items": [{"id": "x"}]}
        return 404, {"error_msg": "no such path"}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.calls.append((self.command, self.path))
                fake.bodies.append(body)
                if not verify(self.command, self.path, self.headers, body, fake.ak, fake.sk):
                    status, data = 401, {"error_code": "APIGW.0301",
                                         "error_msg": "verify aksk signature fail" +
                                                      (" for " + fake.ak if fake.echo_key else "")}
                else:
                    status, data = fake.route(self.command, self.path)
                raw = data.encode() if isinstance(data, str) else json.dumps(data).encode()
                self.send_response(status)
                if status == 429 and fake.retry_after:
                    self.send_header("Retry-After", fake.retry_after)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _serve
            do_POST = _serve
            do_PUT = _serve
            do_DELETE = _serve

        return Handler
