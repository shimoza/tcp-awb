"""Read-only calls to the T Cloud Public (TCP) API with the owner's key.

    awb cloud projects [--region R]
    awb cloud get SERVICE PATH [--region R] [--query K=V]... [--list KEY] [--paging marker|offset|none]
    awb cloud sweep [--region R] [--today YYYY-MM-DD]       untagged, expired or idle resources (awb/tcp/sweep.py)
    awb cloud usage [--month YYYY-MM]                       calls to the TCP API per day and every 429 (awb/tcp/throttle.py)
    awb cloud tenants                                       the tenants of the key service (awb/tcp/keys.py)
    awb cloud call METHOD SERVICE PATH --tenant ALIAS [--role read|lab] [--region R] [--query K=V]... [--body FILE]
                   [--project CODE]                         one call through the key service, for every user
    awb cloud lease-check ALIAS [--region R] [--yes] [--no-terraform]
                                                            the live check of a leased key (awb/tcp/leasecheck.py, owner)

PATH may carry `{project_id}`: the id of the project named like the region. `--list KEY` reads a paged list under
KEY and answers one of three states (awb/jobs.py): list, empty or unknown. The command line sends GET only: a
session checks facts here and changes nothing. The library can send other methods for later jobs.

The key comes from `AWB_CLOUD_KEYS=pass:<entry>`, the entry holding `ak` and `sk` like the key of the bucket. The work
user never holds a key: `call` and `tenants` go through the key service (T-100), which signs for it. `AWB_CLOUD_ENDPOINT` (tests) sends every
service to one base address. Errors carry a status and a short text with the key taken out, never the key.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from awb import config, jobs, obs, sign

DOMAIN = "otc.t-systems.com"
KEYS_ENV = "AWB_CLOUD_KEYS"
ENDPOINT_ENV = "AWB_CLOUD_ENDPOINT"
PAGINGS = ("marker", "offset", "none")
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z0-9]{2,10}$")
_TRANSIENT = (429, 502, 503, 504)


class CloudError(Exception):
    """A call could not be made or answered. The message carries a status and a cleaned text, never a key."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class _Transient(Exception):
    pass


@dataclass
class Response:
    status: int
    data: Any                 # the parsed JSON body, or None
    text: str                 # at most 300 characters of the body, the key taken out

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def check_region(region: str) -> str:
    if not _REGION_RE.match(region or ""):
        raise CloudError("a region reads like eu-de")
    return region


class Client:
    """Signed calls for one region. `endpoint` (tests) replaces https://<service>.<region>.<domain> for every
    service."""

    def __init__(self, keys: obs.Keys, region: str = "eu-de", *, endpoint: str | None = None,
                 timeout: float = 30.0, job: jobs.Job | None = None, label: str = "owner"):
        self.label = label          # the tenant alias in the call log, never an id
        self.keys = keys
        self.region = check_region(region)
        self.endpoint = endpoint.rstrip("/") if endpoint else None
        self.timeout = timeout
        self.job = job
        self._project_id: str | None = None

    def clean(self, text: str) -> str:
        for secret, mark in ((self.keys.sk, "<sk>"), (self.keys.ak, "<ak>")):
            if secret:
                text = text.replace(secret, mark)
        return text

    def base(self, service: str) -> str:
        if not _SERVICE_RE.match(service or ""):
            raise CloudError("a service name reads like ecs or vpc")
        return self.endpoint or "https://%s.%s.%s" % (service, self.region, DOMAIN)

    def url(self, service: str, path: str, query: dict | list | None = None) -> str:
        if not path.startswith("/"):
            raise CloudError("a path starts with a slash")
        q = urllib.parse.urlencode(query or {}, doseq=True)
        return self.base(service) + path + ("?" + q if q else "")

    def request(self, method: str, service: str, path: str, *, query=None, body: Any = None,
                headers: dict[str, str] | None = None) -> Response:
        """One signed call; transient failures (no answer, 429, 502, 503, 504) are tried again within the job's
        budget. Returns the answer of any status; raises CloudError only when there is no answer at all."""
        url = self.url(service, self.expand(path) if "{project_id}" in path else path, query)
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})

        from awb.tcp import throttle

        host = urllib.parse.urlsplit(url).netloc
        tries = {"n": 0}

        def once() -> Response:
            tries["n"] += 1
            waited = throttle.wait_turn(host)
            signed = sign.sign(method, url, hdrs, data, self.keys.ak, self.keys.sk)
            req = urllib.request.Request(url, data=data if body is not None else None, method=method.upper())
            for k, v in signed.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    throttle.log_call(self.label, service, self.region, method.upper(), resp.status, waited)
                    return self._response(resp.status, resp.read())
            except urllib.error.HTTPError as exc:
                throttle.log_call(self.label, service, self.region, method.upper(), exc.code, waited)
                r = self._response(exc.code, exc.read())
                if exc.code in _TRANSIENT:
                    # the gateway may name its own wait: take it before the next try, within the job's budget
                    hint = throttle.retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                    if hint and tries["n"] < 3 and (self.job is None or self.job.remaining() > hint):
                        time.sleep(hint)
                    raise _Transient(r)
                return r
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                throttle.log_call(self.label, service, self.region, method.upper(), 0, waited)
                raise _Transient(CloudError("no answer from the %s endpoint (%s)" % (service, type(exc).__name__)))

        try:
            return jobs.retry(once, attempts=3, delay=2.0, job=self.job, retry_on=(_Transient,))
        except _Transient as exc:
            last = exc.args[0]
            if isinstance(last, Response):
                return last
            raise last from None

    def _response(self, status: int, raw: bytes) -> Response:
        text = self.clean(raw[:300].decode("utf-8", "replace"))
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return Response(status, data, text)

    def get(self, service: str, path: str, query=None) -> Response:
        return self.request("GET", service, path, query=query)

    def project_id(self) -> str:
        """The id of the project named like the region (IAM, /v3/projects?name=<region>)."""
        if self._project_id is None:
            r = self.get("iam", "/v3/projects", {"name": self.region})
            if not r.ok:
                raise CloudError("the project of %s cannot be read: HTTP %d" % (self.region, r.status), r.status)
            found = [p for p in jobs.dig(r.data, "projects", default=[]) or [] if isinstance(p, dict)
                     and p.get("name") == self.region and isinstance(p.get("id"), str)]
            if len(found) != 1:
                raise CloudError("the key reaches %d projects named %s, not one" % (len(found), self.region))
            self._project_id = found[0]["id"]
        return self._project_id

    def expand(self, path: str) -> str:
        return path.replace("{project_id}", self.project_id())

    def list(self, service: str, path: str, key: str, *, query: dict | None = None, paging: str = "marker",
             limit: int = 100, marker_field: str = "id", max_pages: int = 200) -> jobs.Listing:
        """Every item under `key`, page by page: list, empty or unknown. A page that is not an answer with a list
        under `key` makes the whole listing unknown, never empty."""
        if paging not in PAGINGS:
            raise CloudError("paging is marker, offset or none")
        items: list = []
        q = dict(query or {})
        if paging != "none":
            given = q.get("limit")
            given = given[0] if isinstance(given, list) and given else given
            if given is not None:                  # the caller's page size wins over the default of the pager
                try:
                    limit = int(given)
                except (TypeError, ValueError):
                    raise CloudError("limit in the query is a number") from None
            q["limit"] = str(limit)
        for _ in range(max_pages):
            try:
                r = self.get(service, path, q)
            except CloudError as err:
                return jobs.Listing.unknown(str(err))
            if not r.ok:
                return jobs.Listing.unknown("HTTP %d" % r.status)
            page = jobs.dig(r.data, key)
            if not isinstance(page, list):
                return jobs.Listing.unknown("the answer holds no list under %s" % key)
            items.extend(page)
            if paging == "none" or len(page) < limit:
                return jobs.Listing.of(items)
            if paging == "marker":
                last = jobs.dig(page, -1, marker_field)
                if not isinstance(last, str) or not last:
                    return jobs.Listing.unknown("the last item of a page has no %s to go on from" % marker_field)
                q["marker"] = last
            else:
                q["offset"] = str(int(q.get("offset", "0")) + len(page))
        return jobs.Listing.unknown("more than %d pages" % max_pages)


def settings() -> tuple[obs.Keys, str | None]:
    """(keys, endpoint) from the environment of the owner side. Refused as the work user."""
    if config.is_work_user():
        raise CloudError("the work user has no cloud key yet; it gets its own with T-100")
    try:
        keys = obs.keys_from_reference(os.environ.get(KEYS_ENV))
    except obs.OBSError as err:
        raise CloudError("%s: %s" % (KEYS_ENV, err)) from None
    return keys, os.environ.get(ENDPOINT_ENV) or None


def mask(value: str) -> str:
    """The first four characters of an id and an ellipsis."""
    return value[:4] + "..." if len(value) > 4 else value


def _pairs(values: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in values:
        if "=" not in item:
            raise CloudError("a query reads K=V")
        k, v = item.split("=", 1)
        out.setdefault(k, []).append(v)
    return out


def _keys_error():
    from awb.tcp import keys

    return keys.KeysError


def main(argv: list[str] | None = None) -> int:
    """`awb cloud projects|get|sweep`. Exit 0 answered, 1 the API said no (4xx or 5xx), 2 no answer, unknown or usage."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb cloud", description="Read-only calls to the TCP API with the owner's key.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    p = sub.add_parser("projects", help="the projects the key reaches")
    p.add_argument("--region", default="eu-de")
    g = sub.add_parser("get", help="one GET, or a paged list with --list KEY")
    g.add_argument("service")
    g.add_argument("path")
    g.add_argument("--region", default="eu-de")
    g.add_argument("--query", action="append", default=[], metavar="K=V")
    g.add_argument("--list", dest="list_key", default=None, metavar="KEY")
    g.add_argument("--paging", default="marker", choices=PAGINGS)
    g.add_argument("--limit", type=int, default=100)
    w = sub.add_parser("sweep", help="servers, disks and addresses that are untagged, expired or idle (T-61)")
    w.add_argument("--region", default="eu-de")
    w.add_argument("--today", default=None, metavar="YYYY-MM-DD")
    u = sub.add_parser("usage", help="calls to the TCP API per day and every 429, from the call log")
    u.add_argument("--month", default=None, metavar="YYYY-MM")
    sub.add_parser("tenants", help="the tenants of the key service, their keys and the names of their secrets")
    k = sub.add_parser("call", help="one call through the key service: the key never reaches this process")
    k.add_argument("method")
    k.add_argument("service")
    k.add_argument("path")
    k.add_argument("--tenant", required=True)
    k.add_argument("--role", default="read", choices=("read", "lab"))
    k.add_argument("--region", default=None)
    k.add_argument("--query", action="append", default=[], metavar="K=V")
    k.add_argument("--body", default=None, metavar="FILE", help="a JSON file; a password field may hold {{secret:NAME}}")
    k.add_argument("--project", default=None, help="the project code (default: the project of the working folder)")
    lc = sub.add_parser("lease-check", help="the live check of a leased key: P2 and P3 of T12 part 3 (owner side)")
    lc.add_argument("alias")
    lc.add_argument("--region", default=None)
    lc.add_argument("--yes", action="store_true", help="make and delete the throw-away resources (else a dry run)")
    lc.add_argument("--no-terraform", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    if args.command == "usage":
        from awb.tcp import throttle

        month = args.month or datetime.date.today().strftime("%Y-%m")
        if not re.match(r"^\d{4}-\d{2}$", month):
            print("awb cloud: the month reads like 2026-09", file=sys.stderr)
            return 2
        print("\n".join(throttle.usage_lines(config.paths().shared, month)))
        return 0
    if args.command in ("tenants", "call"):
        return _through_service(args)
    if args.command == "lease-check":
        from awb.tcp import leasecheck

        if config.is_work_user():
            print("awb cloud: the lease check is the owner's", file=sys.stderr)
            return 2
        try:
            return leasecheck.main(args.alias, args.region, args.yes, not args.no_terraform)
        except (CloudError, _keys_error()) as err:
            print("awb cloud: %s" % err, file=sys.stderr)
            return 1
    try:
        keys, endpoint = settings()
        job = jobs.Job("cloud", None)
        c = Client(keys, args.region, endpoint=endpoint, job=job)
        if args.command == "sweep":
            from awb.tcp import sweep as _sweep
            try:
                today = datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
            except ValueError:
                raise CloudError("the date reads like 2026-09-25") from None
            handles = _sweep.Handles(_sweep.handles_path(config.paths()))
            rep = _sweep.sweep(lambda service, path, key, paging: c.list(service, path, key, paging=paging),
                               today, handles, _sweep.known_ids(config.paths().projects_root))
            handles.save()
            for line in _sweep.report_lines(rep):
                print(handles.mask(line))
            return 2 if rep.unknown else 0
        if args.command == "projects":
            r = c.get("iam", "/v3/projects")
            if not r.ok:
                print("awb cloud: HTTP %d %s" % (r.status, r.text), file=sys.stderr)
                return 1
            rows = [p for p in jobs.dig(r.data, "projects", default=[]) or [] if isinstance(p, dict)]
            for p in sorted(rows, key=lambda x: str(x.get("name"))):
                print("%-24s %s" % (p.get("name"), mask(str(p.get("id", "")))))
            print("awb cloud: %d project(s)" % len(rows))
            return 0
        query = _pairs(args.query)
        if args.list_key:
            listing = c.list(args.service, args.path, args.list_key, query=query, paging=args.paging,
                             limit=args.limit)
            print("awb cloud: %s" % listing.describe())
            for item in listing.items:
                print(json.dumps(item, ensure_ascii=False, sort_keys=True))
            return 2 if listing.state == jobs.UNKNOWN else 0
        r = c.get(args.service, args.path, query)
        if r.data is not None:
            print(json.dumps(r.data, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(r.text)
        if not r.ok:
            print("awb cloud: HTTP %d" % r.status, file=sys.stderr)
            return 1
        return 0
    except CloudError as err:
        print("awb cloud: %s" % err, file=sys.stderr)
        return 2


def _project_code(arg: str | None) -> str | None:
    if arg:
        return arg
    from awb import review

    scope = review.find_project() / "SCOPE.md"
    try:
        for line in scope.read_text(encoding="utf-8").splitlines():
            if line.startswith("- code:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _through_service(args) -> int:
    from awb.tcp import keys as _keys

    if args.command == "tenants":
        return _keys.main(["status"])
    try:
        body = None
        if args.body:
            try:
                body = json.loads(open(args.body, encoding="utf-8").read())
            except (OSError, ValueError):
                print("awb cloud: the body file is not readable JSON", file=sys.stderr)
                return 2
        req = {"op": "call", "tenant": args.tenant, "role": args.role, "method": args.method.upper(),
               "service": args.service, "path": args.path, "region": args.region, "query": _pairs(args.query),
               "body": body, "project": _project_code(args.project)}
        answer = _keys.request(_keys.call_socket(), req)
    except (_keys.KeysError, CloudError) as err:
        print("awb cloud: %s" % err, file=sys.stderr)
        return 2
    if not answer.get("ok"):
        print("awb cloud: %s" % answer.get("error"), file=sys.stderr)
        return 2
    if answer.get("data") is not None:
        print(json.dumps(answer["data"], ensure_ascii=False, indent=2, sort_keys=True))
    elif answer.get("text"):
        print(answer["text"])
    status = int(answer.get("status") or 0)
    if not 200 <= status < 300:
        print("awb cloud: HTTP %d" % status, file=sys.stderr)
        return 1 if status else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
